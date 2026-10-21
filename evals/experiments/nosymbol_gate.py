"""이름 없는 따라가기 질문 10개가 Supervisor 게이트에서 어디로 가는지 잰다 (before-1). LLM 호출.

supervisor_intents.classify_message(프롬프트 v2 + fast 모델 구조화 출력 + 코드 게이트)를
지도 없음(has_guide=False)·대화 기억 없음(history=[]) 으로 돌린다. 현재 게이트는 식별자 없는 ask 를
map 으로 내리므로, LLM 이 ask 라고 해도 따라가기에 도달하지 못하는 것이 예상 결과다.

실행: uv run python evals/experiments/nosymbol_gate.py
확인만: uv run python evals/experiments/nosymbol_gate.py --dry-run
"""

import argparse
import importlib.util
import json
import time
from datetime import date
from pathlib import Path

from langchain_core.callbacks import UsageMetadataCallbackHandler

from repoute.graphs.supervisor import SYMBOL_PATTERN

HERE = Path(__file__).parent
DATASET = "evals/datasets/walkthrough_questions_nosymbol.json"
OUT = "evals/results/23-nosymbol-before-gate.json"
DEFAULT_TAG = "wt-nosymbol-gate"


def _load(name: str, path: Path):
    """evals/ 는 패키지로 설치되지 않아 import 가 안 되므로 파일 경로로 직접 읽는다."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


intents = _load("supervisor_intents", HERE / "supervisor_intents.py")


def load_dataset(path: str | Path = DATASET) -> list[dict]:
    """데이터셋 json(리스트)을 읽는다."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def symbol_match(question: str) -> str | None:
    """게이트가 식별자로 보는 첫 문자열(SYMBOL_PATTERN). 이 데이터셋에서는 전부 None 이어야 한다."""
    found = SYMBOL_PATTERN.search(question)
    return found.group() if found else None


def run_one(llm, item: dict, tag: str = DEFAULT_TAG) -> dict:
    """질문 하나를 지도 없음·기억 없음으로 분류하고 토큰·비용·지연을 붙인 결과 dict."""
    cb = UsageMetadataCallbackHandler()
    config = {"callbacks": [cb], "tags": [tag, item["id"]]}
    start = time.perf_counter()
    llm_kind, final_kind, gate_reason = intents.classify_message(
        llm, item["question"], [], False, config
    )
    elapsed = time.perf_counter() - start
    tokens = {
        m: {"input_tokens": u["input_tokens"], "output_tokens": u["output_tokens"]}
        for m, u in cb.usage_metadata.items()
    }
    return {
        **{k: item[k] for k in ("id", "repo", "question")},
        "llm_kind": llm_kind,
        "final_kind": final_kind,
        "gate_reason": gate_reason,
        "symbol_match": symbol_match(item["question"]),
        "tokens": tokens,
        "cost": intents.estimate_cost(tokens),
        "elapsed": elapsed,
    }


def summarize(rows: list[dict]) -> dict:
    """ask 도달 수·LLM/게이트 후 kind 분포·게이트 발동·식별자 매치·비용. 실패(error) 행은 뺀다."""
    ok = [r for r in rows if "error" not in r]
    n = len(ok)
    costs = [r["cost"] for r in ok]
    total_cost = None if not ok or None in costs else sum(costs)
    return {
        "n": n,
        "failed": len(rows) - n,
        "reached_ask": sum(r["final_kind"] == "ask" for r in ok),
        "llm_ask": sum(r["llm_kind"] == "ask" for r in ok),
        "llm_kinds": {k: sum(r["llm_kind"] == k for r in ok) for k in intents.KINDS},
        "final_kinds": {k: sum(r["final_kind"] == k for r in ok) for k in intents.KINDS},
        "gate_applied": sum(r["gate_reason"] is not None for r in ok),
        "symbol_matches": [r["id"] for r in ok if r["symbol_match"] is not None],
        "total_cost": total_cost,
        "avg_elapsed": intents._avg([r["elapsed"] for r in ok]),
    }


def render_table(rows: list[dict], summary: dict) -> str:
    """표준출력용 표 + 요약 줄. 기록 파일은 사람이 이 표를 보고 쓴다."""
    s, total = summary, summary["n"] + summary["failed"]
    lines = ["| id | 질문 | LLM | 게이트 후 | 이유 |", "|---|---|---|---|---|"]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['id']} | {r['question'][:30]} | 실패 | | {r['error']} |")
            continue
        lines.append(
            f"| {r['id']} | {r['question'][:30]} | {r['llm_kind']} | {r['final_kind']} "
            f"| {r['gate_reason'] or '—'} |"
        )
    kinds = ", ".join(f"{k} {v}" for k, v in s["llm_kinds"].items() if v)
    lines += [
        "",
        f"- **ask 로 따라가기에 도달한 수**: {s['reached_ask']}/{total}",
        f"- LLM 단독 ask {s['llm_ask']}/{s['n']} (LLM 분포: {kinds or '—'}), "
        f"게이트 발동 {s['gate_applied']}건",
        "- SYMBOL_PATTERN 매치: "
        + (", ".join(s["symbol_matches"]) if s["symbol_matches"] else "없음 (전부 None)"),
        f"- 비용 합 {intents._money(s['total_cost'])}"
        + (f", 실패 {s['failed']}건" if s["failed"] else ""),
    ]
    return "\n".join(lines)


def dry_run(items: list[dict]) -> None:
    """LLM 없이 항목 수·예상 호출 수·대략 비용만 출력한다 (supervisor_intents 와 같은 토큰 가정)."""
    model = "gpt-4o-mini"
    each = intents.estimate_cost({model: intents.DRY_RUN_TOKENS})
    print(f"질문 {len(items)}개, 예상 LLM 호출 {len(items)}회 ({model}, 질문당 1회)")
    print(f"대략 비용 ${each * len(items):.4f} (질문당 ${each:.6f}, 입력 ~500·출력 ~40 토큰 가정)")
    matched = [it["id"] for it in items if symbol_match(it["question"])]
    print(f"SYMBOL_PATTERN 매치: {', '.join(matched) if matched else '없음'}")


def main() -> None:
    """질문마다 한 번씩 분류하고 raw json 저장 + 표를 표준출력에 찍는다."""
    parser = argparse.ArgumentParser(description="이름 없는 질문의 Supervisor 게이트 (LLM 호출)")
    parser.add_argument("--dataset", default=DATASET, help="질문 json")
    parser.add_argument("--out", default=OUT, help="raw 결과 json")
    parser.add_argument("--tag", default=DEFAULT_TAG, help="LangSmith 태그")
    parser.add_argument("--dry-run", action="store_true", help="LLM 호출 없이 개수·비용 추정만")
    args = parser.parse_args()
    items = load_dataset(args.dataset)
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
            print(f"[ok] {row['id']}: 최종 {row['final_kind']}{gate}")
        except Exception as e:  # 한 항목이 실패해도 나머지는 계속 잰다
            message = str(e).strip().splitlines()[0] if str(e).strip() else ""
            row = {**item, "error": f"{type(e).__name__}: {message}"}
            print(f"[fail] {item['id']}: {row['error']}")
        rows.append(row)

    summary = summarize(rows)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    meta = {"model": model, "date": date.today().isoformat(), "tag": args.tag}
    payload = {**meta, "dataset": args.dataset, "has_guide": False, "history": []}
    payload |= {"summary": summary, "rows": rows}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(render_table(rows, summary))
    print(f"\n저장: {out}")


if __name__ == "__main__":
    main()
