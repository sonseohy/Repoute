"""Supervisor 의도 분류 기준선: 메시지 30개의 LLM 단독·게이트 후 정확도와 비용. 실제 LLM 호출.

그래프를 돌리지 않고 supervisor.classify 와 같은 로직(프롬프트 v2 + fast 모델 구조화 출력
+ 코드 게이트)을 재현한다. 인덱싱·서브그래프가 없어 분류 호출만 잰다.

실행: uv run python evals/experiments/supervisor_intents.py
확인만: uv run python evals/experiments/supervisor_intents.py --dry-run
"""

import argparse
import json
import time
from datetime import date
from pathlib import Path

from langchain_core.callbacks import UsageMetadataCallbackHandler

from repoute.graphs.supervisor import SYMBOL_PATTERN, Intent, _gate, _history_text
from repoute.prompts import supervisor_v2

DATASET = "evals/datasets/supervisor_intents.json"
OUT = "evals/results/22-ui-baselines-intents.json"
DEFAULT_TAG = "ui-baseline-intents"
KINDS = ("map", "ask", "next", "chat")
# walkthrough_batch.py 와 같은 단가(USD / 1M 토큰). 스크립트는 패키지가 아니라 복사해 둔다.
PRICES = {"gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.00)}
# --dry-run 비용 가정: 분류 프롬프트(약 1,000자) + 기억 몇 줄 ≈ 입력 500 토큰, Intent 출력 ≈ 40 토큰
DRY_RUN_TOKENS = {"input_tokens": 500, "output_tokens": 40}


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


def load_dataset(path: str | Path = DATASET) -> list[dict]:
    """데이터셋 json(리스트)을 읽는다."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def is_symbolless_ask(item: dict) -> bool:
    """사람 라벨은 ask 인데 메시지에 식별자가 없는 항목 (현재 게이트가 map 으로 내리는 경우)."""
    return item["expected"] == "ask" and not SYMBOL_PATTERN.search(item["message"])


def classify_message(
    llm, message: str, history: list[dict], has_guide: bool, config: dict | None = None
) -> tuple[str, str, str | None]:
    """supervisor.classify 와 같은 분류를 한 번 한다. (LLM kind, 게이트 후 kind, 게이트 이유|None).

    게이트가 같은 kind 를 돌려주면 classify 처럼 '바꾸지 않은 것' 으로 보고 이유는 None.
    """
    prompt = supervisor_v2.CLASSIFY.format(history=_history_text(history), message=message)
    intent = llm.with_structured_output(Intent).invoke(prompt, config=config)
    gate = _gate(intent.kind, message, has_guide)
    if gate and gate[0] != intent.kind:
        return intent.kind, gate[0], gate[1]
    return intent.kind, intent.kind, None


def run_one(llm, item: dict, tag: str) -> dict:
    """항목 하나를 분류하고 토큰·비용·지연을 붙인 결과 dict 를 돌려준다."""
    cb = UsageMetadataCallbackHandler()
    config = {"callbacks": [cb], "tags": [tag, item["id"]]}
    start = time.perf_counter()
    llm_kind, final_kind, gate_reason = classify_message(
        llm, item["message"], item["history"], item["has_guide"], config
    )
    elapsed = time.perf_counter() - start
    tokens = {
        m: {"input_tokens": u["input_tokens"], "output_tokens": u["output_tokens"]}
        for m, u in cb.usage_metadata.items()
    }
    return {
        **{k: item[k] for k in ("id", "message", "has_guide", "expected")},
        "llm_kind": llm_kind,
        "final_kind": final_kind,
        "gate_reason": gate_reason,
        "llm_correct": llm_kind == item["expected"],
        "final_correct": final_kind == item["expected"],
        "symbolless_ask": is_symbolless_ask(item),
        "tokens": tokens,
        "cost": estimate_cost(tokens),
        "elapsed": elapsed,
    }


def _avg(values: list) -> float | None:
    """평균. 모르는 값(None)이 하나라도 있으면 None (측정 안 한 수치 금지)."""
    return None if not values or None in values else sum(values) / len(values)


def summarize(rows: list[dict]) -> dict:
    """정확도·kind 별 정확도·혼동 행렬·게이트 효과·비용을 모은다. 실패(error) 행은 뺀다."""
    ok = [r for r in rows if "error" not in r]
    n = len(ok)
    per_kind = {}
    for kind in KINDS:
        group = [r for r in ok if r["expected"] == kind]
        per_kind[kind] = {
            "total": len(group),
            "llm_correct": sum(r["llm_correct"] for r in group),
            "final_correct": sum(r["final_correct"] for r in group),
        }
    # 행 = 사람 라벨(expected), 열 = 게이트 후 최종 kind
    confusion = {e: {f: 0 for f in KINDS} for e in KINDS}
    for r in ok:
        confusion[r["expected"]][r["final_kind"]] += 1
    costs = [r["cost"] for r in ok]
    total_cost = None if not ok or None in costs else sum(costs)
    llm_hit = sum(r["llm_correct"] for r in ok)
    final_hit = sum(r["final_correct"] for r in ok)
    return {
        "n": n,
        "failed": len(rows) - n,
        "llm_correct": llm_hit,
        "final_correct": final_hit,
        "llm_accuracy": llm_hit / n if n else None,
        "final_accuracy": final_hit / n if n else None,
        "per_kind": per_kind,
        "confusion": confusion,
        "gate_applied": sum(r["gate_reason"] is not None for r in ok),
        "gate_fixed": sum(not r["llm_correct"] and r["final_correct"] for r in ok),
        "gate_broke": sum(r["llm_correct"] and not r["final_correct"] for r in ok),
        "symbolless_ask": [
            {k: r[k] for k in ("id", "message", "llm_kind", "final_kind", "gate_reason")}
            for r in ok
            if r["symbolless_ask"]
        ],
        "total_cost": total_cost,
        "avg_cost": None if total_cost is None else total_cost / n,
        "avg_elapsed": _avg([r["elapsed"] for r in ok]),
    }


def _money(x: float | None) -> str:
    return "—" if x is None else f"${x:.5f}"


def _frac(hit: int, total: int) -> str:
    return f"{hit}/{total} ({hit / total:.0%})" if total else "0/0"


def render_markdown(rows: list[dict], summary: dict, model: str = "") -> str:
    """표준출력용 마크다운 표. 기록 파일(22)은 사람이 이 표를 보고 쓴다."""
    s, n = summary, summary["n"]
    lines = [
        f"## Supervisor 의도 분류 기준선 ({model or 'fast'}, {date.today().isoformat()})",
        "",
        f"- n = {n} (실패 {s['failed']}), 데이터 `{DATASET}`",
        f"- **LLM 단독 정확도**: {_frac(s['llm_correct'], n)}",
        f"- **게이트 후 정확도**: {_frac(s['final_correct'], n)}",
        f"- 게이트 발동 {s['gate_applied']}건: 틀림→맞음 {s['gate_fixed']}, "
        f"맞음→틀림 {s['gate_broke']}",
        f"- 비용 합 {_money(s['total_cost'])}, 호출당 평균 {_money(s['avg_cost'])}, "
        "평균 지연 " + ("—" if s["avg_elapsed"] is None else f"{s['avg_elapsed']:.2f}s"),
        "",
        "### kind 별 (맞은 수/전체)",
        "",
        "| expected | n | LLM 단독 | 게이트 후 |",
        "|---|---|---|---|",
    ]
    for kind, k in s["per_kind"].items():
        lines.append(
            f"| {kind} | {k['total']} | {_frac(k['llm_correct'], k['total'])} "
            f"| {_frac(k['final_correct'], k['total'])} |"
        )
    lines += ["", "### 혼동 행렬 (행 = expected, 열 = 게이트 후)", ""]
    lines += ["| expected \\ final | " + " | ".join(KINDS) + " |"]
    lines += ["|---" * (len(KINDS) + 1) + "|"]
    for e, cols in s["confusion"].items():
        lines.append(f"| {e} | " + " | ".join(str(cols[f]) for f in KINDS) + " |")
    lines += ["", "### 함수 이름 없는 ask (Phase 5.5-4 완화 대상)", ""]
    lines += ["| id | 메시지 | LLM | 게이트 후 |", "|---|---|---|---|"]
    for r in s["symbolless_ask"]:
        lines.append(f"| {r['id']} | {r['message']} | {r['llm_kind']} | {r['final_kind']} |")
    lines += ["", "### 항목별", ""]
    lines += ["| id | 메시지 | 지도 | expected | LLM | 게이트 후 | 맞음 | 비용 | 지연 |"]
    lines += ["|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['id']} | {r['message'][:30]} | | {r['expected']} | 실패 | | | | |")
            continue
        lines.append(
            f"| {r['id']} | {r['message'][:30]} | {'O' if r['has_guide'] else '-'} "
            f"| {r['expected']} | {r['llm_kind']} | {r['final_kind']} "
            f"| {'O' if r['final_correct'] else 'X'} | {_money(r['cost'])} "
            f"| {r['elapsed']:.2f}s |"
        )
    failed = [r for r in rows if "error" in r]
    if failed:
        lines += ["", "### 실패", ""] + [f"- {r['id']}: {r['error']}" for r in failed]
    return "\n".join(lines)


def dry_run(items: list[dict]) -> None:
    """LLM 없이 항목 수·예상 호출 수·대략 비용만 출력한다."""
    model = "gpt-4o-mini"
    each = estimate_cost({model: DRY_RUN_TOKENS})
    print(f"항목 {len(items)}개, 예상 LLM 호출 {len(items)}회 ({model}, 항목당 1회)")
    print(
        f"대략 비용 ${each * len(items):.4f} (항목당 입력 ~{DRY_RUN_TOKENS['input_tokens']} "
        f"토큰·출력 ~{DRY_RUN_TOKENS['output_tokens']} 토큰 가정, 항목당 ${each:.6f})"
    )


def main() -> None:
    """항목마다 한 번씩 분류하고 raw json 저장 + 마크다운 표를 표준출력에 찍는다."""
    parser = argparse.ArgumentParser(description="Supervisor 의도 분류 기준선 (LLM 호출)")
    parser.add_argument("--dataset", default=DATASET, help="메시지 json")
    parser.add_argument("--ids", nargs="+", help="일부 항목 id 만 (예: si-01 si-13)")
    parser.add_argument("--out", default=OUT, help="raw 결과 json")
    parser.add_argument("--tag", default=DEFAULT_TAG, help="LangSmith 태그")
    parser.add_argument("--dry-run", action="store_true", help="LLM 호출 없이 개수·비용 추정만")
    args = parser.parse_args()
    items = load_dataset(args.dataset)
    if args.ids:
        items = [it for it in items if it["id"] in args.ids]
    if args.dry_run:
        return dry_run(items)

    from dotenv import load_dotenv

    load_dotenv(".env")  # troubleshooting 06: 경로를 명시해야 키가 읽힌다
    from repoute.config import get_fast_llm, load_settings

    llm, model = get_fast_llm(), load_settings().fast_model
    rows = []
    for item in items:
        try:
            row = run_one(llm, item, args.tag)
            gate = f" (게이트: {row['llm_kind']}→{row['final_kind']})" if row["gate_reason"] else ""
            mark = "O" if row["final_correct"] else "X"
            print(f"[{mark}] {row['id']}: 기대 {row['expected']}, 최종 {row['final_kind']}{gate}")
        except Exception as e:  # 한 항목이 실패해도 나머지는 계속 잰다
            message = str(e).strip().splitlines()[0] if str(e).strip() else ""
            row = {**item, "error": f"{type(e).__name__}: {message}"}
            print(f"[fail] {item['id']}: {row['error']}")
        rows.append(row)

    summary = summarize(rows)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    meta = {"model": model, "date": date.today().isoformat(), "tag": args.tag}
    payload = {**meta, "dataset": args.dataset, "summary": summary, "rows": rows}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(render_markdown(rows, summary, model))
    print(f"\n저장: {out}")


if __name__ == "__main__":
    main()
