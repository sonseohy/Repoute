"""따라가기 배치: 질문 20개(레포 4 × 5)의 인용 유효성·도달·비용을 기록한다. 실제 LLM 호출.

실행: uv run python evals/experiments/walkthrough_batch.py
확인만: uv run python evals/experiments/walkthrough_batch.py --dry-run (LLM·인덱스 없음)
"""

import argparse
import importlib
import importlib.util
import json
import time
from datetime import date
from pathlib import Path

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from repoute.config import get_embedder, get_fast_llm, get_quality_llm, load_settings
from repoute.graphs.deps import Deps
from repoute.graphs.walkthrough import (
    EXPLORE_BLOCK,
    MAX_NUDGES,
    MAX_RETRIES,
    MAX_TOOL_CALLS,
    RECURSION_LIMIT,
    build_walkthrough_graph,
)
from repoute.indexing.embeddings import ensure_embeddings
from repoute.indexing.index import build_index
from repoute.validation.citations import citation_at_def, citation_specificity, citation_validity

DATASET = "evals/datasets/walkthrough_questions.json"
DEFAULT_TITLE = "10. 따라가기 배치 — 질문 20개"
DEFAULT_TAG = "wt-batch"
# e4_validation_loop.py 와 같은 단가(USD / 1M 토큰). 스크립트는 패키지가 아니라 복사해 둔다.
PRICES = {"gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.00)}
# --dry-run 비용 추정: 기록 13 의 질문당 평균(LangSmith). 평균이라 질문에 따라 넘을 수 있다.
COST_PER_QUESTION = {"quality": 0.083, "fast": 0.018}
# 실행되지 않은 도구 호출의 ToolMessage 머리말: 이미 읽은 범위 차단·상한 미실행·탐색 차단
BLOCKED_PREFIXES = ("이미 읽은 범위", "실행 안 함", EXPLORE_BLOCK.split("{")[0])
BLOCKED_MARK = "!blocked"  # tool_sequence 에서 실행 안 된 호출 표시
ERROR_MARK = "!error"  # 실행했지만 "도구 오류" 가 난 호출 표시 (읽기로 치지 않는다)
HERE = Path(__file__).parent


def _load(name: str, path: Path):
    """evals/ 는 패키지로 설치되지 않아 import 가 안 되므로 파일 경로로 직접 읽는다."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# rescore_batch 의 expect_file 과, 그것이 읽어 둔 Phase 4 평가자(metrics)를 같이 쓴다
rescore = _load("rescore_batch", HERE / "rescore_batch.py")
metrics = rescore.metrics


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


def tool_sequence(messages: list) -> list[str]:
    """agent 가 낸 도구 호출 이름을 AIMessage.tool_calls 순서대로 나열한다.

    도구 층에서 실행되지 않은 호출(이미 읽은 범위·상한·탐색 차단, 또는 답 ToolMessage 없음)은
    이름 뒤에 "!blocked", 실행했지만 "도구 오류" 가 난 호출은 "!error" 를 붙인다.
    예: ["find_definition", "read_code", "read_code!blocked"]
    """
    results = {m.tool_call_id: str(m.content) for m in messages if isinstance(m, ToolMessage)}
    sequence = []
    for m in messages:
        if not isinstance(m, AIMessage):
            continue
        for call in m.tool_calls:
            content = results.get(call["id"])
            if content is None or content.startswith(BLOCKED_PREFIXES):
                sequence.append(call["name"] + BLOCKED_MARK)
            elif content.startswith("도구 오류"):
                sequence.append(call["name"] + ERROR_MARK)
            else:
                sequence.append(call["name"])
    return sequence


def calls_before_first_read(sequence: list[str]) -> int | None:
    """첫 실행된 read_code 앞에 agent 가 낸 도구 호출 수. 실행된 read_code 가 없으면 None.

    차단된 호출도 agent 가 낸 시도라 센다(예산도 소모). 차단·오류 read_code 는 읽기가 아니다.
    """
    return sequence.index("read_code") if "read_code" in sequence else None


def chain_accuracy(refs: list[tuple[str, str | None]], item: dict) -> float | None:
    """인용 순서로 만든 예측 체인과 item["expect_chain"] 의 일치도. expect_chain 이 없으면 None."""
    expect = item.get("expect_chain")
    if not expect:
        return None
    return metrics.call_chain_accuracy(metrics.predicted_chain_from_refs(refs), expect)


def reached_tail(answer, item: dict) -> bool:
    """metrics.reached(끝 이름 매칭) 도달. 기존 reached(문자열 포함)와 달리 짧은 이름도 인정."""
    refs = [(ref.file, ref.symbol) for step in answer.steps for ref in step.refs]
    text = "\n".join(f"{step.title}\n{step.explanation}" for step in answer.steps)
    return metrics.reached(refs, text, item["expect_reach"], rescore.expect_file(item))


def run_one(
    item: dict,
    agent_model: str,
    prompt: str,
    cache_dir,
    tag: str = DEFAULT_TAG,
    search_symbols: bool = False,
) -> dict:
    """질문 하나를 새 상태로 돌리고(체크포인터 없음) 최종 답을 채점해 지표 dict 를 돌려준다."""
    prompts = importlib.import_module(f"repoute.prompts.walkthrough_{prompt}")
    index = build_index(f"https://github.com/{item['repo']}", cache_dir)
    try:
        embedder = store = None
        if search_symbols:  # RAG 1단계: 심볼 임베딩 검색 도구를 6번째로 (첫 레포만 임베딩 비용)
            settings = load_settings()
            embedder = get_embedder(settings)
            store = ensure_embeddings(index, embedder, settings.embedding_model, cache_dir)
        deps = Deps(get_fast_llm(), get_quality_llm(), index, embedder, store)
        graph = build_walkthrough_graph(deps, prompts, checkpointer=None, agent_model=agent_model)
        cb = UsageMetadataCallbackHandler()  # 그래프 안 모든 LLM 호출의 토큰을 모델별로 합산
        config = {
            "callbacks": [cb],
            "tags": [tag, agent_model, prompt, item["repo"]]
            + (["search-symbols"] if search_symbols else []),
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
            "explore_blocked": 0,
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
        sequence = tool_sequence(state["messages"])
        pairs = [(r.file, r.symbol) for r in refs]
        tokens = {
            m: {"input_tokens": u["input_tokens"], "output_tokens": u["output_tokens"]}
            for m, u in cb.usage_metadata.items()
        }
        return {
            **{k: item[k] for k in ("id", "repo", "question", "expect_reach")},
            "agent_model": agent_model,
            "prompt": prompt,
            "search_symbols": search_symbols,
            "embeddings_cached": None if store is None else store.cached,
            "tool_calls": state.get("tool_calls", 0),
            "retries": state.get("retry_count", 0),
            "blocked": len(blocked),
            "read_code_calls": read_code_calls(state["messages"]),
            "nudges": state.get("nudges", 0),
            "explore_blocked": state.get("explore_blocked", 0),
            "final_errors": final_errors,
            # 그래프 검증(존재 + 읽은 범위)을 최종적으로 통과했고 인용이 하나 이상 있는가
            "strict_pass": final_errors == 0 and len(refs) > 0,
            "steps": len(answer.steps),
            "citations": len(refs),
            "validity": citation_validity(refs, index.db, root),
            "specificity": citation_specificity(refs, index.db, root),
            "at_def": citation_at_def(refs, index.db, root),
            "reached": reached(answer, item["expect_reach"]),
            "reached_tail": reached_tail(answer, item),
            "chain_accuracy": chain_accuracy(pairs, item),
            "tool_sequence": sequence,
            "calls_before_first_read": calls_before_first_read(sequence),
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


def _first_read(group: list[dict]) -> str:
    """첫 읽기까지 호출 수 평균(None 제외) + 읽기 없는 건수. 이 지표가 없던 raw 면 "—"."""
    if any("calls_before_first_read" not in r for r in group):
        return "—"
    values = [r["calls_before_first_read"] for r in group]
    known = [v for v in values if v is not None]
    avg = f"{sum(known) / len(known):.1f}" if known else "—"
    return f"{avg} (읽기 없음 {len(values) - len(known)}건)"


def _chain(group: list[dict]) -> str:
    """체인 정확도 평균(expect_chain 이 있는 질문만). 일부만 있으면 n 을 붙인다."""
    known = [r["chain_accuracy"] for r in group if r.get("chain_accuracy") is not None]
    if not known:
        return "—"
    avg = f"{sum(known) / len(known):.2f}"
    return avg if len(known) == len(group) else f"{avg} (n={len(known)})"


def _extra(group: list[dict]) -> str:
    """요약 표 뒤에 붙는 첫읽기까지 | 체인 칸 (기존 칸 순서는 그대로 둔다)."""
    return f" {_first_read(group)} | {_chain(group)} |"


def _cell_first_read(r: dict) -> str:
    if "calls_before_first_read" not in r:
        return "—"
    v = r["calls_before_first_read"]
    return "없음" if v is None else str(v)


def _cell_chain(r: dict) -> str:
    v = r.get("chain_accuracy")
    return "—" if v is None else f"{v:.2f}"


def _cell_tail(r: dict) -> str:
    return "—" if "reached_tail" not in r else ("O" if r["reached_tail"] else "X")


def _summary(group: list[dict]) -> str:
    """유효 | 구체 | def줄 | 도달 | 도구 평균 | 비용 평균 | 시간 평균 칸."""
    avg = {k: _avg([r[k] for r in group]) for k in ("validity", "specificity", "at_def")}
    return (
        f"| {_pct(avg['validity'])} | {_pct(avg['specificity'])} | {_pct(avg['at_def'])} "
        f"| {_reach(group)} | {_avg([r['tool_calls'] for r in group]):.1f} "
        f"| {_money(_avg([r['cost'] for r in group]))} "
        f"| {_avg([r['elapsed'] for r in group]):.1f}s |"
    )


def render_markdown(
    rows: list[dict], title: str = DEFAULT_TITLE, dataset: str = DATASET, tag: str = DEFAULT_TAG
) -> str:
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
        f"- **n = {len(ok)}** 질문 (`{dataset}`, 레포 {len({r['repo'] for r in ok})}개), "
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
        "- **탐색차단**: 읽기 없이 탐색만 6회 한 뒤 실행하지 않은 find_* 수(예산은 소모).",
        "- **비용**: `UsageMetadataCallbackHandler` 토큰 × 스크립트 `PRICES` 단가 추정치. "
        f"LangSmith 태그 `{tag}` 로 교차 확인. **시간**: invoke 벽시계(인덱싱 제외).",
        "- **첫읽기**: 첫 실행된 read_code 앞에 agent 가 낸 도구 호출 수(차단 포함). "
        '"없음" = 실행된 read_code 가 없음. 평균은 읽은 질문만.',
        "- **체인**: `metrics.call_chain_accuracy(predicted_chain_from_refs(인용), expect_chain)` "
        "(인용 순서 심볼 vs 기대 호출 체인, 끝 이름 LCS / 기대 길이).",
        "- **끝도달**: `metrics.reached`(인용 심볼 끝 이름 매칭, 흔한 이름은 파일·한정자 확인). "
        "**도달** 은 기존 문자열 포함 판정 그대로.",
        '- **도구 호출 순서**: AIMessage.tool_calls 순서. "!blocked" = 도구 층에서 실행 안 됨, '
        '"!error" = 도구 오류.',
        "",
        "## 질문별 결과",
        "",
        "| id | 질문 | 도구 | 읽기 | 차단 | 탐색차단 | 단계 | 인용 | 엄격 | 유효 | 구체 | def줄 "
        "| 도달 | 비용 | 시간 | 첫읽기 | 체인 | 끝도달 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in ok:
        lines.append(
            f"| {r['id']} | {r['question'][:40]} | {r['tool_calls']} | {r['read_code_calls']} "
            f"| {r['blocked']} | {r.get('explore_blocked', '—')} | {r['steps']} | {r['citations']} "
            f"| {'O' if r['strict_pass'] else 'X'} | {_pct(r['validity'])} "
            f"| {_pct(r['specificity'])} | {_pct(r['at_def'])} | {'O' if r['reached'] else 'X'} "
            f"| {_money(r['cost'])} | {r['elapsed']:.1f}s | {_cell_first_read(r)} "
            f"| {_cell_chain(r)} | {_cell_tail(r)} |"
        )
    lines += ["", "## 레포별 평균", ""]
    lines += ["| 레포 | n | 유효 | 구체 | def줄 | 도달 | 도구 | 비용 | 시간 | 첫읽기까지 | 체인 |"]
    lines += ["|---|---|---|---|---|---|---|---|---|---|---|"]
    for repo in dict.fromkeys(r["repo"] for r in ok):
        group = [r for r in ok if r["repo"] == repo]
        lines.append(f"| {repo} | n={len(group)} " + _summary(group) + _extra(group))
    if ok:
        costs = [r["cost"] for r in ok]
        total = None if None in costs else sum(costs)
        lines += ["", "## 전체 요약", ""]
        lines += [
            "| n | 유효 | 구체 | def줄 | 도달 | 도구 | 비용 평균 | 시간 | 비용 합 "
            "| 첫읽기까지 | 체인 |"
        ]
        lines += ["|---|---|---|---|---|---|---|---|---|---|---|"]
        lines.append(f"| n={len(ok)} " + _summary(ok) + f" {_money(total)} |" + _extra(ok))
        strict = sum(r["strict_pass"] for r in ok)
        unread = sum(r["read_code_calls"] == 0 for r in ok)
        explored = sum(r.get("explore_blocked", 0) > 0 for r in ok)
        lines += ["", f"- **엄격 통과 비율**: {strict}/{len(ok)} ({strict / len(ok):.0%})"]
        lines += [f"- **read_code 0회 건수**: {unread}/{len(ok)}"]
        lines += [f"- **탐색 차단 발동 건수**: {explored}/{len(ok)}"]
        if all("reached_tail" in r for r in ok):
            tail = sum(r["reached_tail"] for r in ok)
            differ = [r["id"] for r in ok if r["reached_tail"] != r["reached"]]
            note = f" (기존 도달과 다른 질문: {', '.join(differ)})" if differ else ""
            lines += [f"- **끝 이름 도달**: {tail}/{len(ok)} ({tail / len(ok):.0%}){note}"]
        sequences = [r for r in ok if "tool_sequence" in r]
        if sequences:
            lines += ["", "## 도구 호출 순서", ""]
            lines += [
                f"- {r['id']}: " + (" → ".join(r["tool_sequence"]) or "(호출 없음)")
                for r in sequences
            ]
    failed = [r for r in rows if "error" in r]
    if failed:
        lines += ["", "## 실패", ""] + [f"- {r['id']} ({r['repo']}): {r['error']}" for r in failed]
    lines += ["", "## 관찰", "", "- ", "- ", ""]
    return "\n".join(lines)


def dry_run(items: list[dict], agent_model: str, tag: str, prompt: str) -> None:
    """LLM·인덱스 없이 항목 수·레포·예상 호출 수·비용 추정만 출력한다."""
    repos = {}
    for it in items:
        repos[it["repo"]] = repos.get(it["repo"], 0) + 1
    agent_max = 1 + MAX_TOOL_CALLS + MAX_NUDGES  # 첫 agent + 도구 단계마다 1 + 넛지 뒤 1
    finalize_max = MAX_RETRIES + 1
    each = agent_max + finalize_max
    per_q = COST_PER_QUESTION[agent_model]
    print(
        f"질문 {len(items)}개, 레포 {len(repos)}개 ("
        + ", ".join(f"{repo} {n}" for repo, n in repos.items())
        + ")"
    )
    print(
        f"agent = {agent_model}, 프롬프트 {prompt}. 질문당 LLM 호출 최대 {each}회 "
        f"(agent ≤{agent_max} + finalize ≤{finalize_max}), 도구 호출 ≤{MAX_TOOL_CALLS}회 "
        f"→ 전체 LLM 호출 최대 {each * len(items)}회"
    )
    print(
        f"예상 비용 ≈ ${per_q * len(items):.2f} (기록 13 질문당 평균 {agent_model} ${per_q:.3f} "
        f"× {len(items)}; 평균이라 실제는 넘을 수 있음. "
        + ", ".join(f"{k} ${v:.3f}" for k, v in COST_PER_QUESTION.items())
        + ")"
    )
    print(f"LangSmith 태그: [{tag}, {agent_model}, {prompt}, <repo>]")


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
    parser.add_argument("--tag", default=DEFAULT_TAG, help="LangSmith 태그 (config tags 첫 칸)")
    parser.add_argument(
        "--search-symbols",
        action="store_true",
        help="심볼 임베딩 검색 도구(search_symbols)를 6번째로 켠다 (레포당 첫 1회 임베딩 비용)",
    )
    parser.add_argument("--json-out", help="raw json 경로 (기본: --out 의 .json)")
    parser.add_argument("--dry-run", action="store_true", help="LLM 호출 없이 개수·비용 추정만")
    args = parser.parse_args()
    items = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    if args.ids:
        items = [it for it in items if it["id"] in args.ids]
    out = Path(args.out)
    json_out = Path(args.json_out) if args.json_out else out.with_suffix(".json")
    if args.dry_run:
        dry_run(items, args.agent_model, args.tag, args.prompt)
        print(f"저장 예정: {out} , {json_out}")
        return

    rows = []
    for item in items:
        try:
            row = run_one(
                item, args.agent_model, args.prompt, args.cache_dir, args.tag, args.search_symbols
            )
            print(
                f"[ok] {row['id']}: 도구 {row['tool_calls']}, 읽기 {row['read_code_calls']}, "
                f"첫읽기 {_cell_first_read(row)}, 엄격 {'O' if row['strict_pass'] else 'X'}, "
                f"유효 {row['validity']:.0%}, 도달 {'O' if row['reached'] else 'X'}"
                f"/끝 {_cell_tail(row)}, 체인 {_cell_chain(row)}, {_money(row['cost'])}, "
                f"{row['elapsed']:.1f}s"
            )
        except Exception as e:  # 한 질문이 실패해도 나머지는 계속 잰다
            message = str(e).strip().splitlines()[0] if str(e).strip() else ""
            row = {**item, "prompt": args.prompt, "agent_model": args.agent_model}
            row["error"] = f"{type(e).__name__}: {message}"
            print(f"[fail] {item['id']}: {row['error']}")
        rows.append(row)

    out.parent.mkdir(parents=True, exist_ok=True)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    out.write_text(render_markdown(rows, args.title, args.dataset, args.tag), encoding="utf-8")
    print(f"저장: {out} , {json_out}")


if __name__ == "__main__":
    main()
