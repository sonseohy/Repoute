"""PR Coach 정적 단계 평가: gold PR 마다 변경 심볼·영향 범위·테스트 힌트를 재현해 정답과 비교.

LLM·MCP 는 부르지 않는다. PR 의 변경 파일은 evals/datasets/raw/pr_files_<owner>__<name>_<n>.json
(REST get_files 응답)에서 읽고, 그래프와 같은 함수(parse_changed_files → find_changed_symbols →
find_impact → find_test_files)를 그대로 쓴다. 레포는 build_index 로 clone·인덱싱한다(캐시).

지표 (심볼은 끝 이름 비교):
- changed_recall: gold changed_symbols 중 예측 변경 심볼에 든 비율
- impact_recall: gold_impact_symbols 중 예측 영향 심볼에 든 비율 (metrics.impact_recall)
- impact_precision: 예측 "호출자" 중 gold 에 든 비율 (분모 = 호출자 예측 수, 0개면 0.0).
  피호출자는 기본 0개라 v1(분모 = 전체 예측 수)과 같은 식이다. 켜도 정밀도 분모에는 넣지 않는다.
- test_file_hit: 예측 테스트 파일 중 하나라도 gold_test_files 와 파일명이 같은가

실행:
  uv run python evals/experiments/pr_coach_eval.py --dry-run   # 대상·raw 유무만
  uv run python evals/experiments/pr_coach_eval.py             # 결과 → evals/results/26-...json
  uv run python evals/experiments/pr_coach_eval.py --out evals/results/26-pr-coach-gold-v2.json
"""

import argparse
import importlib.util
import json
import time
from datetime import date
from pathlib import Path

from repoute.github.prs import ChangedFile, parse_changed_files
from repoute.graphs.pr_coach import find_changed_symbols, find_impact, find_test_files

DATASET = "evals/datasets/pr_coach_gold.json"
RAW_DIR = "evals/datasets/raw"
DEFAULT_OUT = "evals/results/26-pr-coach-gold.json"
DIFFICULTIES = ("easy", "medium", "hard")
METRIC_KEYS = ("changed_recall", "impact_recall", "impact_precision", "test_file_hit")
METRICS = Path(__file__).resolve().parents[1] / "evaluators" / "metrics.py"


def _load_metrics():
    """evals/ 는 패키지가 아니라 metrics.py 를 파일 경로로 읽는다 (다른 실험 스크립트와 같다)."""
    spec = importlib.util.spec_from_file_location("metrics", METRICS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = _load_metrics()


# ---- raw 변경 파일 (네트워크 없음) ----


def raw_path(raw_dir: Path | str, repo: str, number: int) -> Path:
    """레포 "owner/name" 의 PR 번호 raw 파일 경로 (pr_files_owner__name_<n>.json)."""
    owner, _, name = repo.partition("/")
    return Path(raw_dir) / f"pr_files_{owner}__{name}_{number}.json"


def load_files(raw_dir: Path | str, repo: str, number: int) -> list[ChangedFile] | None:
    """raw get_files 응답 → ChangedFile 목록 (그래프와 같은 파서). 파일이 없으면 None."""
    path = raw_path(raw_dir, repo, number)
    if not path.is_file():
        return None
    return parse_changed_files(path.read_text(encoding="utf-8"))


# ---- 예측·지표 ----


def predict(index, files: list[ChangedFile]) -> dict:
    """정적 단계만 재현: 변경 심볼 → 영향(호출자, 피호출자는 기본 0) → 테스트 파일."""
    start = time.perf_counter()
    changed, notes = find_changed_symbols(index, files)
    impact = find_impact(index.db, changed)
    tests = find_test_files(Path(index.info.path), [s.name for s in changed])
    return {
        "changed": [s.model_dump() for s in changed],
        "impact": [s.model_dump() for s in impact],
        "test_files": tests,
        "notes": notes,
        "seconds": round(time.perf_counter() - start, 3),
    }


def _tail(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def impact_precision(predicted_symbols: list[str], gold_symbols: list[str]) -> float:
    """예측 영향 심볼 중 정답(끝 이름)에 든 비율. 분모는 예측 수(중복 이름은 한 번), 0개면 0.0."""
    pred_tails = {_tail(p) for p in predicted_symbols}
    if not pred_tails:
        return 0.0
    gold_tails = {_tail(g) for g in gold_symbols}
    return len(pred_tails & gold_tails) / len(pred_tails)


def score(pred: dict, item: dict) -> dict:
    """gold 한 항목에 대한 지표 dict (METRIC_KEYS)."""
    changed = [s["name"] for s in pred["changed"]]
    impact = [s["name"] for s in pred["impact"]]
    callers = [s["name"] for s in pred["impact"] if s["relation"] == "caller"]
    gold_changed = [s["name"] for s in item["changed_symbols"]]
    gold_impact = [s["name"] for s in item["gold_impact_symbols"]]
    return {
        # 변경 심볼 재현율도 "정답 중 예측에 든 비율" 이라 impact_recall 과 같은 식이다
        "changed_recall": m.impact_recall(changed, gold_changed),
        "impact_recall": m.impact_recall(impact, gold_impact),
        "impact_precision": impact_precision(callers, gold_impact),  # 호출자만 분모
        "test_file_hit": m.test_file_hit(pred["test_files"], item["gold_test_files"]),
    }


def evaluate_item(index, item: dict, files: list[ChangedFile]) -> dict:
    """gold 한 항목: 예측 → 지표. 결과 행 dict (표·집계 입력)."""
    pred = predict(index, files)
    return {
        "id": item["id"],
        "repo": item["repo"],
        "number": item["number"],
        "title": item.get("title", ""),
        "difficulty": item["difficulty"],
        **pred,
        "metrics": score(pred, item),
    }


def _rate(values: list) -> float | None:
    """평균 (bool 은 비율). 빈 목록이면 None."""
    return sum(values) / len(values) if values else None


def summarize(rows: list[dict]) -> dict:
    """행 목록의 요약: n·failed·지표 평균. 오류 행(error 키)은 n 에서 빼고 failed 로 센다."""
    ok = [r for r in rows if "error" not in r]
    return {
        "n": len(ok),
        "failed": len(rows) - len(ok),
        **{key: _rate([r["metrics"][key] for r in ok]) for key in METRIC_KEYS},
    }


def aggregate(rows: list[dict]) -> dict:
    """PR 별 행 → {"by_repo", "by_difficulty", "overall"}. 행이 없는 난이도는 뺀다."""
    repos = dict.fromkeys(r["repo"] for r in rows)
    return {
        "by_repo": {repo: summarize([r for r in rows if r["repo"] == repo]) for repo in repos},
        "by_difficulty": {
            d: summarize([r for r in rows if r["difficulty"] == d])
            for d in DIFFICULTIES
            if any(r["difficulty"] == d for r in rows)
        },
        "overall": summarize(rows),
    }


# ---- 표 ----


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.0%}"


def render_pr_table(rows: list[dict]) -> str:
    """PR 별 표: id, 번호, 변경 수, 영향 수, 변경 재현, 영향 재현·정밀, 테스트 hit."""
    lines = [
        "| id | # | 변경 | 영향 | 변경 재현 | 영향 재현 | 영향 정밀 | 테스트 hit |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['id']} | {r['number']} | {r['error']} ||||||")
            continue
        x = r["metrics"]
        lines.append(
            f"| {r['id']} | {r['number']} | {len(r['changed'])} | {len(r['impact'])} "
            f"| {x['changed_recall']:.2f} | {x['impact_recall']:.2f} "
            f"| {x['impact_precision']:.2f} | {'O' if x['test_file_hit'] else 'X'} |"
        )
    return "\n".join(lines)


def render_summary_table(label: str, groups: dict[str, dict]) -> str:
    """요약 표 하나 (레포별·난이도별·전체)."""
    lines = [
        f"| {label} | n | 변경 재현 | 영향 재현 | 영향 정밀 | 테스트 hit |",
        "|---|---|---|---|---|---|",
    ]
    for key, s in groups.items():
        n = f"{s['n']}" + (f" (실패 {s['failed']})" if s["failed"] else "")
        cells = " | ".join(_pct(s[k]) for k in METRIC_KEYS)
        lines.append(f"| {key} | {n} | {cells} |")
    return "\n".join(lines)


# ---- 실행 ----


def dry_run(dataset: Path, raw_dir: str) -> None:
    """gold·raw 파일 유무와 대상 PR 만 출력한다 (인덱스·API 호출 없음)."""
    if not dataset.is_file():
        print(f"파일 없음: {dataset} (gold 를 먼저 만드세요)")
        return
    items = json.loads(dataset.read_text(encoding="utf-8"))
    repos = dict.fromkeys(item["repo"] for item in items)
    print(f"PR {len(items)}개, 레포 {len(repos)}개 (LLM·MCP 호출 0, 레포 clone·인덱싱은 캐시)")
    for item in items:
        path = raw_path(raw_dir, item["repo"], item["number"])
        state = "있음" if path.is_file() else "파일 없음"
        print(f"- {item['id']} {item['repo']}#{item['number']}: raw {state} ({path.name})")


def _error_row(item: dict, error: str) -> dict:
    """실패한 gold 항목의 행 (표에 id·번호는 보이게)."""
    keys = ("id", "repo", "number", "difficulty")
    return {**{k: item[k] for k in keys}, "title": item.get("title", ""), "error": error}


def main() -> None:
    """gold PR 마다 정적 단계 → 지표, .json 저장, PR 별·요약 표를 출력한다."""
    parser = argparse.ArgumentParser(description="PR Coach 정적 단계 평가 (LLM·MCP 없음)")
    parser.add_argument("--dataset", default=DATASET, help="pr_coach_gold.json")
    parser.add_argument("--raw-dir", default=RAW_DIR, help="pr_files_<owner>__<name>_<n>.json 폴더")
    parser.add_argument("--cache-dir", default=".repoute_cache", help="clone·인덱스 캐시")
    parser.add_argument("--out", default=DEFAULT_OUT, help="raw 결과 json")
    parser.add_argument("--dry-run", action="store_true", help="대상·raw 유무만 출력")
    args = parser.parse_args()

    dataset = Path(args.dataset)
    if args.dry_run:
        dry_run(dataset, args.raw_dir)
        return
    if not dataset.is_file():
        print(f"파일 없음: {dataset}")
        return

    from repoute.indexing.index import build_index

    items = json.loads(dataset.read_text(encoding="utf-8"))
    rows, repo_meta = [], {}
    for repo in dict.fromkeys(item["repo"] for item in items):
        index = build_index(f"https://github.com/{repo}", args.cache_dir)
        repo_meta[repo] = {"repo_id": index.info.repo_id, "commit": index.info.commit}
        try:
            for item in (i for i in items if i["repo"] == repo):
                files = load_files(args.raw_dir, repo, item["number"])
                if files is None:
                    rows.append(_error_row(item, "raw 없음"))
                    continue
                try:
                    rows.append(evaluate_item(index, item, files))
                except Exception as e:  # 한 PR 이 실패해도 나머지는 계속 잰다
                    text = str(e).strip()
                    message = text.splitlines()[0] if text else ""
                    rows.append(_error_row(item, f"{type(e).__name__}: {message}"))
        finally:
            index.db.close()

    summary = aggregate(rows)
    result = {
        "meta": {
            "date": date.today().isoformat(),
            "dataset": args.dataset,
            "raw_dir": args.raw_dir,
            "stages": "parse_changed_files → find_changed_symbols → find_impact → find_test_files",
            "changed_symbols": "hunk 문맥 이름 우선, 없으면 첫 변경 줄(시작 + 3)의 감싸는 심볼",
            "impact": "변경 심볼마다 호출자 ≤5, 피호출자 0 (find_impact 기본값)",
            "llm_calls": 0,
            "repos": repo_meta,
        },
        "rows": rows,
        "summary": summary,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(render_pr_table(rows))
    print()
    print(render_summary_table("레포", summary["by_repo"]))
    print()
    print(render_summary_table("난이도", summary["by_difficulty"]))
    print()
    print(render_summary_table("전체", {"전체": summary["overall"]}))
    print()
    print("- 심볼은 끝 이름 비교, 테스트 hit 는 파일명(경로 끝) 비교.")
    print("- 줄 번호는 PR 의 새 파일 기준, 심볼은 인덱스 커밋 기준 (커밋 차이로 어긋날 수 있다).")
    print("- 영향 정밀은 호출자 예측만 분모 (피호출자는 기본 0개라 이전 기록과 같은 식).")
    print(f"저장: {out}")


if __name__ == "__main__":
    main()
