"""따라가기 배치 raw(.json)를 Phase 4 평가자(끝 이름 도달·호출 체인 정확도)로 다시 채점한다.

LLM·네트워크 없음. 결과 json 과 데이터셋 json 만 읽는다.
실행: uv run python evals/experiments/rescore_batch.py evals/results/1*-walkthrough-batch*.json
"""

import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parents[2]
DATASET = ROOT / "evals" / "datasets" / "walkthrough_questions.json"


def _load_metrics():
    """evals/ 는 패키지로 설치되지 않아 import 가 안 되므로 파일 경로로 직접 읽는다."""
    path = ROOT / "evals" / "evaluators" / "metrics.py"
    spec = importlib.util.spec_from_file_location("metrics", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


metrics = _load_metrics()


def parse_ref(text: str) -> tuple[str, str | None]:
    """배치 raw 의 "file:line [symbol]" 또는 "file:line" 문자열 → (file, symbol 또는 None)."""
    where, _, symbol = text.partition(" [")
    file = where.rsplit(":", 1)[0]
    return file, (symbol.rstrip("]") or None)


def expect_file(item: dict) -> str | None:
    """기대 도달 심볼의 파일. 데이터셋에 명시가 없으면 None (흔한 이름은 한정자로만 판정)."""
    if item.get("expect_file"):
        return item["expect_file"]
    evidence = item.get("chain_evidence") or []
    last = evidence[-1] if evidence else None
    return last.get("file") if isinstance(last, dict) else None


def rescore_row(row: dict, item: dict) -> dict:
    """row 하나의 기존 도달·새 도달·체인 정확도(없으면 None).

    raw 에는 답 본문(제목·설명)이 저장돼 있지 않다. 기존 판정이 바로 "본문·인용에 expect 전체가
    들어 있는가" 였으므로 새 규칙 1번(텍스트 포함)은 기존 판정으로 대신한다(OR).
    """
    refs = [parse_ref(t) for t in row.get("refs", [])]
    expect = item.get("expect_reach", row["expect_reach"])
    new = row["reached"] or metrics.reached(refs, "", expect, expect_file(item))
    chain = item.get("expect_chain")
    acc = (
        metrics.call_chain_accuracy(metrics.predicted_chain_from_refs(refs), chain)
        if chain
        else None
    )
    return {"id": row["id"], "old": row["reached"], "new": new, "chain_acc": acc}


def report(path: Path, items: dict[str, dict]) -> None:
    """json 하나의 질문별 표와 요약을 출력한다. 실패(error) row 는 건너뛴다."""
    rows = [r for r in json.loads(path.read_text(encoding="utf-8")) if "error" not in r]
    scored = [rescore_row(r, items.get(r["id"], {})) for r in rows]
    print(f"== {path.name} (n={len(scored)})\n")
    print("| id | 기존 reached | 새 reached | chain acc |\n|---|---|---|---|")
    for s in scored:
        acc = "—" if s["chain_acc"] is None else f"{s['chain_acc']:.2f}"
        print(f"| {s['id']} | {'O' if s['old'] else 'X'} | {'O' if s['new'] else 'X'} | {acc} |")
    n = len(scored)
    old, new = sum(s["old"] for s in scored), sum(s["new"] for s in scored)
    accs = [s["chain_acc"] for s in scored if s["chain_acc"] is not None]
    mean = f"{sum(accs) / len(accs):.2f} (n={len(accs)})" if accs else "— (expect_chain 없음)"
    print(f"\n- 도달: 기존 {old}/{n} ({old / n:.0%}) → 새 {new}/{n} ({new / n:.0%})")
    print(f"- chain acc 평균: {mean}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="따라가기 배치 재채점 (LLM 없음)")
    parser.add_argument("paths", nargs="+", type=Path, help="배치 raw json (기록 10~13)")
    parser.add_argument("--dataset", type=Path, default=DATASET, help="질문 json")
    args = parser.parse_args()
    items = {it["id"]: it for it in json.loads(args.dataset.read_text(encoding="utf-8"))}
    for path in args.paths:
        report(path, items)


if __name__ == "__main__":
    main()
