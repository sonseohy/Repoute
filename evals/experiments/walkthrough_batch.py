"""따라가기 배치: 질문 20개(레포 4 × 5)의 인용 유효성·도달·비용을 기록한다. 실제 LLM 호출.

실행: uv run python evals/experiments/walkthrough_batch.py
"""

import argparse
import importlib
import json
import time
from datetime import date
from pathlib import Path

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from repoute.config import get_fast_llm, get_quality_llm, load_settings
from repoute.graphs.deps import Deps
from repoute.graphs.walkthrough import RECURSION_LIMIT, build_walkthrough_graph
from repoute.indexing.index import build_index
from repoute.validation.citations import citation_at_def, citation_specificity, citation_validity

DATASET = "evals/datasets/walkthrough_questions.json"
DEFAULT_TITLE = "10. 따라가기 배치 — 질문 20개"
# e4_validation_loop.py 와 같은 단가(USD / 1M 토큰). 스크립트는 패키지가 아니라 복사해 둔다.
PRICES = {"gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.00)}


def _price(model: str) -> tuple[float, float] | None:
    """모델 단가. 'gpt-4o-mini-2024-07-18' 처럼 날짜가 붙어도 최장 접두어로 찾는다."""
    keys = [k for k in PRICES if model == k or model.startswith(k + "-")]
    return PRICES[max(keys, key=len)] if keys else None


def estimate_cost(usage: dict) -> float | None:
    """{모델: {"input_tokens", "output_tokens"}} 의 비용(USD). 단가 모르는 모델·빈 값이면 None."""
    prices = [_price(m) for m in usage]
    if not usage or None in prices:
        return None  # 토큰을 못 쟀거나 단가를 모르면 0 이 아니라 '모름'
    return sum(
        (u["input_tokens"] * p[0] + u["output_tokens"] * p[1]) / 1_000_000
        for u, p in zip(usage.values(), prices, strict=True)
    )


def reached(answer, expect: str) -> bool:
    """기대 도달점(심볼 이름)이 어떤 단계의 제목·설명 또는 인용 심볼에 들어 있으면 True.

    문자열 포함 여부만 보는 자동 근사치다. 인용 심볼을 "send" 처럼 짧게 쓰고 본문에도
    "Session.send" 를 안 쓰면 실제로 도달했어도 False 가 된다 (사람이 표를 보고 보정).
    """
    for step in answer.steps:
        symbols = [ref.symbol or "" for ref in step.refs]
        if any(expect in text for text in [step.title, step.explanation, *symbols]):
            return True
    return False


def read_code_calls(messages: list) -> int:
    """실제로 실행된 read_code 호출 수. "이미 읽은 범위" 차단과 "실행 안 함"(상한)은 뺀다."""
    ids = {
        c["id"]
        for m in messages
        if isinstance(m, AIMessage)
        for c in m.tool_calls
        if c["name"] == "read_code"
    }
    skipped = ("이미 읽은 범위", "실행 안 함")
    return sum(
        1
        for m in messages
        if isinstance(m, ToolMessage)
        and m.tool_call_id in ids
        and not str(m.content).startswith(skipped)
    )


def run_one(item: dict, agent_model: str, prompt: str, cache_dir) -> dict:
    """질문 하나를 새 상태로 돌리고(체크포인터 없음) 최종 답을 채점해 지표 dict 를 돌려준다."""
    prompts = importlib.import_module(f"repoute.prompts.walkthrough_{prompt}")
    index = build_index(f"https://github.com/{item['repo']}", cache_dir)
    try:
        deps = Deps(get_fast_llm(), get_quality_llm(), index)
        graph = build_walkthrough_graph(deps, prompts, checkpointer=None, agent_model=agent_model)
        cb = UsageMetadataCallbackHandler()  # 그래프 안 모든 LLM 호출의 토큰을 모델별로 합산
        config = {
            "callbacks": [cb],
            "tags": ["wt-batch", agent_model, prompt, item["repo"]],
            "configurable": {"thread_id": item["id"]},
            "recursion_limit": RECURSION_LIMIT,
        }
        # ask() 는 config 를 받지 않으므로 같은 초기 상태로 직접 invoke 한다
        start_state = {
            "messages": [HumanMessage(item["question"])],
            "tool_calls": 0,
            "retry_count": 0,
            "validation_errors": [],
            "nudges": 0,
        }
        start = time.perf_counter()
        state = graph.invoke(start_state, config=config)
        elapsed = time.perf_counter() - start
        answer, root = state["answer"], index.info.path
        refs = [ref for step in answer.steps for ref in step.refs]
        blocked = [
            m
            for m in state["messages"]
            if isinstance(m, ToolMessage) and str(m.content).startswith("이미 읽은 범위")
        ]
        final_errors = len(state.get("validation_errors") or [])
        tokens = {
            m: {"input_tokens": u["input_tokens"], "output_tokens": u["output_tokens"]}
            for m, u in cb.usage_metadata.items()
        }
        return {
            **{k: item[k] for k in ("id", "repo", "question", "expect_reach")},
            "agent_model": agent_model,
            "prompt": prompt,
            "tool_calls": state.get("tool_calls", 0),
            "retries": state.get("retry_count", 0),
            "blocked": len(blocked),
            "read_code_calls": read_code_calls(state["messages"]),
            "nudges": state.get("nudges", 0),
            "final_errors": final_errors,
            # 그래프 검증(존재 + 읽은 범위)을 최종적으로 통과했고 인용이 하나 이상 있는가
            "strict_pass": final_errors == 0 and len(refs) > 0,
            "steps": len(answer.steps),
            "citations": len(refs),
            "validity": citation_validity(refs, index.db, root),
            "specificity": citation_specificity(refs, index.db, root),
            "at_def": citation_at_def(refs, index.db, root),
            "reached": reached(answer, item["expect_reach"]),
            "refs": [f"{r.file}:{r.line}" + (f" [{r.symbol}]" if r.symbol else "") for r in refs],
            "tokens": tokens,
            "cost": estimate_cost(tokens),
            "elapsed": elapsed,
        }
    finally:
        index.db.close()


def _money(x: float | None) -> str:
    return "—" if x is None else f"${x:.4f}"


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.0%}"


def _avg(values: list) -> float | None:
    """평균. 모르는 값(None)이 하나라도 있으면 None (측정 안 한 수치 금지)."""
    return None if not values or None in values else sum(values) / len(values)


def _reach(group: list[dict]) -> str:
    hit = sum(r["reached"] for r in group)
    return f"{hit}/{len(group)} ({hit / len(group):.0%})"


def _summary(group: list[dict]) -> str:
    """유효 | 구체 | def줄 | 도달 | 도구 평균 | 비용 평균 | 시간 평균 칸."""
    avg = {k: _avg([r[k] for r in group]) for k in ("validity", "specificity", "at_def")}
    return (
        f"| {_pct(avg['validity'])} | {_pct(avg['specificity'])} | {_pct(avg['at_def'])} "
        f"| {_reach(group)} | {_avg([r['tool_calls'] for r in group]):.1f} "
        f"| {_money(_avg([r['cost'] for r in group]))} "
        f"| {_avg([r['elapsed'] for r in group]):.1f}s |"
    )


def render_markdown(rows: list[dict], title: str = DEFAULT_TITLE) -> str:
    """raw 결과를 실험 기록 마크다운으로 만든다. '관찰' 은 사람이 채운다."""
    s = load_settings()
    ok = [r for r in rows if "error" not in r]
    agents = sorted({r.get("agent_model", "fast") for r in rows})
    agent_names = ", ".join(s.fast_model if a == "fast" else s.quality_model for a in agents)
    versions = ", ".join(sorted({r["prompt"] for r in rows}))
    lines = [
        f"# {title} (Phase 3, {date.today().isoformat()})",
        "",
        "## 방법",
        "",
        f"- **n = {len(ok)}** 질문 (`{DATASET}`, 레포 {len({r['repo'] for r in ok})}개), "
        "질문마다 체크포인터 없이 새 상태로 실행.",
        f"- **모델**: agent = {agent_names} ({', '.join(agents)}), finalize = {s.quality_model} "
        f"({s.provider}), temperature 0. 프롬프트 {versions}.",
        "- **유효·구체·def줄**: 최종 답 인용을 `citation_validity`·`citation_specificity`·"
        "`citation_at_def` 로 사후 채점.",
        "- **엄격**: 최종 `validation_errors` 가 0개이고 인용이 1개 이상(그래프 검증 = 존재 + "
        "read_code 로 읽은 범위 안). 사후 유효성은 파일:줄 존재만 보므로 "
        "엄격 통과를 주 지표로 본다.",
        '- **읽기**: 실제 실행된 read_code 호출 수("이미 읽은 범위" 차단·상한 미실행 제외).',
        "- **도달**: `expect_reach`(사람이 코드를 보고 정한 기대 도달 심볼)가 어떤 단계의 "
        "제목·설명 또는 인용 심볼 문자열에 들어 있는지 보는 **자동 근사치**. 사람 판정 아님.",
        '- **차단**: "이미 읽은 범위" 로 막힌 read_code 수. **도구**: 실행된 도구 호출 수.',
        "- **비용**: `UsageMetadataCallbackHandler` 토큰 × 스크립트 `PRICES` 단가 추정치. "
        "LangSmith 태그 `wt-batch` 로 교차 확인. **시간**: invoke 벽시계(인덱싱 제외).",
        "",
        "## 질문별 결과",
        "",
        "| id | 질문 | 도구 | 읽기 | 차단 | 단계 | 인용 | 엄격 | 유효 | 구체 | def줄 | 도달 "
        "| 비용 | 시간 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in ok:
        lines.append(
            f"| {r['id']} | {r['question'][:40]} | {r['tool_calls']} | {r['read_code_calls']} "
            f"| {r['blocked']} | {r['steps']} | {r['citations']} "
            f"| {'O' if r['strict_pass'] else 'X'} | {_pct(r['validity'])} "
            f"| {_pct(r['specificity'])} | {_pct(r['at_def'])} | {'O' if r['reached'] else 'X'} "
            f"| {_money(r['cost'])} | {r['elapsed']:.1f}s |"
        )
    lines += ["", "## 레포별 평균", ""]
    lines += ["| 레포 | n | 유효 | 구체 | def줄 | 도달 | 도구 | 비용 | 시간 |"]
    lines += ["|---|---|---|---|---|---|---|---|---|"]
    for repo in dict.fromkeys(r["repo"] for r in ok):
        group = [r for r in ok if r["repo"] == repo]
        lines.append(f"| {repo} | n={len(group)} " + _summary(group))
    if ok:
        costs = [r["cost"] for r in ok]
        total = None if None in costs else sum(costs)
        lines += ["", "## 전체 요약", ""]
        lines += ["| n | 유효 | 구체 | def줄 | 도달 | 도구 | 비용 평균 | 시간 | 비용 합 |"]
        lines += ["|---|---|---|---|---|---|---|---|---|"]
        lines.append(f"| n={len(ok)} " + _summary(ok) + f" {_money(total)} |")
        strict = sum(r["strict_pass"] for r in ok)
        unread = sum(r["read_code_calls"] == 0 for r in ok)
        lines += ["", f"- **엄격 통과 비율**: {strict}/{len(ok)} ({strict / len(ok):.0%})"]
        lines += [f"- **read_code 0회 건수**: {unread}/{len(ok)}"]
    failed = [r for r in rows if "error" in r]
    if failed:
        lines += ["", "## 실패", ""] + [f"- {r['id']} ({r['repo']}): {r['error']}" for r in failed]
    lines += ["", "## 관찰", "", "- ", "- ", ""]
    return "\n".join(lines)


def main() -> None:
    """질문마다 한 번씩 돌리고 .json(raw)·.md(요약)로 저장한다. 실패는 기록하고 계속한다."""
    parser = argparse.ArgumentParser(description="따라가기 배치 측정 (LLM 호출)")
    parser.add_argument("--dataset", default=DATASET, help="질문 json")
    parser.add_argument("--ids", nargs="+", help="일부 질문 id 만 (예: requests-01 rich-05)")
    parser.add_argument("--agent-model", choices=["fast", "quality"], default="fast")
    parser.add_argument("--prompt", choices=["v1", "v2", "v3"], default="v3", help="프롬프트 버전")
    parser.add_argument("--cache-dir", default=".repoute_cache", help="clone·인덱스 캐시")
    parser.add_argument("--out", default="evals/results/10-walkthrough-batch.md")
    parser.add_argument("--title", default=DEFAULT_TITLE, help="결과 마크다운 제목 (번호. 실험명)")
    args = parser.parse_args()
    items = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    if args.ids:
        items = [it for it in items if it["id"] in args.ids]

    rows = []
    for item in items:
        try:
            row = run_one(item, args.agent_model, args.prompt, args.cache_dir)
            print(
                f"[ok] {row['id']}: 도구 {row['tool_calls']}, 읽기 {row['read_code_calls']}, "
                f"엄격 {'O' if row['strict_pass'] else 'X'}, 유효 {row['validity']:.0%}, "
                f"도달 {'O' if row['reached'] else 'X'}, {_money(row['cost'])}, "
                f"{row['elapsed']:.1f}s"
            )
        except Exception as e:  # 한 질문이 실패해도 나머지는 계속 잰다
            message = str(e).strip().splitlines()[0] if str(e).strip() else ""
            row = {**item, "prompt": args.prompt, "agent_model": args.agent_model}
            row["error"] = f"{type(e).__name__}: {message}"
            print(f"[fail] {item['id']}: {row['error']}")
        rows.append(row)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    out.write_text(render_markdown(rows, args.title), encoding="utf-8")
    print(f"저장: {out} , {out.with_suffix('.json')}")


if __name__ == "__main__":
    main()
