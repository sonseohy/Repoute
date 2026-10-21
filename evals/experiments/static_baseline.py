"""Phase 4 베이스라인: LLM 없는 정적 휴리스틱(진입점 탐지, PageRank)을 gold 와 비교한다.

실행: uv run python evals/experiments/static_baseline.py   (LLM 호출 없음, 인덱스 캐시 사용)
"""

import importlib.util
import json
from pathlib import Path

from repoute.indexing.entrypoints import find_entrypoints
from repoute.indexing.imports import rank_modules
from repoute.indexing.index import build_index

GOLD = Path("evals/datasets/reading_guide_gold.json")
METRICS = Path(__file__).with_name("..") / "evaluators" / "metrics.py"


def _metrics():
    """evals/ 는 패키지가 아니라 파일 경로로 읽는다 (troubleshooting 13)."""
    spec = importlib.util.spec_from_file_location("metrics", METRICS.resolve())
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    m = _metrics()
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    recalls, recalls5, precisions = [], [], []
    print("| 레포 | entrypoint_recall | recall@5 | core_module_p@5 | 진입점 수 | PageRank 상위 5 |")
    print("|---|---|---|---|---|---|")
    for g in gold:
        index = build_index(f"https://github.com/{g['repo']}")
        predicted_eps = [
            {"file": s.file, "symbol": s.name} for s in find_entrypoints(index.db, index.info.path)
        ]
        top5 = [name for name, _ in rank_modules(index.graph)[:5]]
        recall = m.entrypoint_recall(predicted_eps, g["entrypoints"])  # 상위 10 기준
        recall5 = m.entrypoint_recall(predicted_eps[:5], g["entrypoints"])
        precision = m.core_module_precision_at_k(top5, g["core_modules"], 5)
        recalls.append(recall)
        recalls5.append(recall5)
        precisions.append(precision)
        short = ", ".join(x.rsplit(".", 1)[-1] for x in top5)
        print(
            f"| {g['repo']} | {recall:.2f} | {recall5:.2f} | {precision:.2f} "
            f"| {len(predicted_eps)} | {short} |"
        )
        index.db.close()
    n = len(gold)
    print(
        f"\n평균 (n={n}): entrypoint_recall={sum(recalls) / n:.2f}  "
        f"recall@5={sum(recalls5) / n:.2f}  "
        f"core_module_precision@5={sum(precisions) / n:.2f}"
    )


if __name__ == "__main__":
    main()
