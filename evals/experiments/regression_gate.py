"""CI 회귀 평가: 읽기 지도를 레포 × validate × 프롬프트 v3 로 돌려 기준치 아래면 exit 1.

LangSmith 없이 돈다(LANGSMITH_TRACING 이 켜져 있으면 트레이스만 남는다). 지표는 결정론적인 것만
(gold 대비 진입점 recall·핵심 모듈 p@5, 인용 유효 비율). 실제 LLM 을 호출한다(레포당 약 $0.02).
실행: uv run python evals/experiments/regression_gate.py --cache-dir .repoute_cache
      uv run python evals/experiments/regression_gate.py --dry-run   # LLM 없이 판정 경로만
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
GOLD = ROOT / "evals" / "datasets" / "reading_guide_gold.json"
# 기록 18 기준(1.0 / 0.68 / 0.32), 회귀 감지용. 올리는 것은 측정 후.
THRESHOLDS = {"citation_validity": 1.0, "core_module_p5": 0.5, "entrypoint_recall": 0.2}
# dry-run 용 가짜 row: 기록 18 의 레포별 수치 그대로 (레포, recall, p@5, 인용 유효 비율)
DRY_ROWS = [
    dict(zip(("repo", "entrypoint_recall", "core_module_p5", "citation_validity"), t, strict=True))
    for t in [
        ("psf/requests@dry-run", 0.33, 0.8, 1.0),
        ("encode/httpx@dry-run", 0.0, 0.6, 1.0),
        ("pallets/click@dry-run", 0.25, 1.0, 1.0),
        ("fastapi/fastapi@dry-run", 0.0, 0.8, 1.0),
        ("Textualize/rich@dry-run", 1.0, 0.2, 1.0),
    ]
]


def summarize(rows: list[dict]) -> dict[str, float | None]:
    """지표별 평균. 실패한 레포·빈 값이 하나라도 있으면 None (측정 못 한 것은 통과가 아니다)."""
    summary = {}
    for key in THRESHOLDS:
        values = [r.get(key) for r in rows]
        summary[key] = None if not values or None in values else sum(values) / len(values)
    return summary


def passes(summary: dict, thresholds: dict) -> list[str]:
    """기준치에 못 미친(또는 측정 못 한) 지표 이름 목록. 비어 있으면 통과."""
    return [k for k, t in thresholds.items() if summary.get(k) is None or summary[k] < t]


def _fmt(x: float | None) -> str:
    return "—" if x is None else f"{x:.2f}"


def _line(row: dict) -> str:
    """레포별 한 줄. 실패한 레포는 오류 메시지."""
    if "error" in row:
        return f"[fail] {row['repo']}: {row['error']}"
    scores = ", ".join(f"{k} {_fmt(row.get(k))}" for k in THRESHOLDS)
    extra = f", ${row['cost']:.4f}" if row.get("cost") is not None else ""
    extra += f" (gold 커밋 {row['gold_commit']} 과 다름)" if row.get("gold_commit") else ""
    return f"[ok] {row['repo']}: {scores}{extra}"


def run(repos: list[str], gold: dict, cache_dir: str) -> list[dict]:
    """레포마다 e4.run_one(validate, v3). e4 는 LangChain 을 import 하므로 실제 실행 때만 읽는다."""
    path = ROOT / "evals" / "experiments" / "e4_validation_loop.py"
    spec = importlib.util.spec_from_file_location("e4_validation_loop", path)
    e4 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(e4)
    rows = []
    for repo in repos:
        try:
            row = e4.run_one(repo, True, cache_dir, "v3", gold[repo])  # 안에서 score_against_gold
            row["citation_validity"] = row["valid"]
            commit, want = row["repo"].rsplit("@", 1)[-1], gold[repo]["commit"]
            if not (want.startswith(commit) or commit.startswith(want)):
                row["gold_commit"] = want  # 점수는 내되, 레포가 gold 이후로 바뀌었음을 알린다
        except Exception as e:  # 한 레포가 실패해도 나머지는 잰다 (평균은 None → FAIL)
            row = {"repo": repo, "error": f"{type(e).__name__}: {e}"}
        print(_line(row), flush=True)
        rows.append(row)
    return rows


def load_gold() -> dict[str, dict]:
    """gold 를 {"owner/name": 항목} 으로 (e4.load_gold 와 같은 일, e4 를 안 읽으려고 따로 둔다)."""
    return {g["repo"]: g for g in json.loads(GOLD.read_text(encoding="utf-8"))}


def main() -> None:
    """레포별 한 줄 → 평균 → PASS/FAIL. FAIL 이면 exit 1."""
    gold = load_gold()
    parser = argparse.ArgumentParser(description="읽기 지도 회귀 평가 (LLM 호출)")
    parser.add_argument("--repos", nargs="+", choices=list(gold), default=list(gold))
    parser.add_argument("--cache-dir", default=".repoute_cache", help="clone·인덱스 캐시")
    parser.add_argument("--dry-run", action="store_true", help="LLM 없이 가짜 row 로 판정만")
    args = parser.parse_args()
    if args.dry_run:
        rows = DRY_ROWS
        for row in rows:
            print(_line(row))
    else:
        rows = run(args.repos, gold, args.cache_dir)
    summary = summarize(rows)
    print(f"평균 (n={len(rows)}): " + ", ".join(f"{k} {_fmt(v)}" for k, v in summary.items()))
    failed = passes(summary, THRESHOLDS)
    if failed:
        print("FAIL: " + ", ".join(f"{k} {_fmt(summary[k])} < {THRESHOLDS[k]}" for k in failed))
        sys.exit(1)
    print("PASS: " + ", ".join(f"{k} >= {t}" for k, t in THRESHOLDS.items()))


if __name__ == "__main__":
    main()
