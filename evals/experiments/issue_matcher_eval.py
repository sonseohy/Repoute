"""Issue Matcher 매칭 단계(RAG) 평가: gold 20개 이슈를 임베딩 검색만으로 모듈·심볼에 매칭.

MCP·LLM 은 부르지 않는다. 이슈 텍스트는 evals/datasets/raw/issues_<owner>__<name>*.json 에서
gold 의 number 로 찾고, issue_matcher.match_symbols 와 같은 쿼리(제목 + "\\n" + 본문 앞
QUERY_BODY 자)·같은 모듈 변환(_module_names)을 쓴다. 비용은 이슈마다 임베딩 쿼리 1회
(20회 ≈ $0.0001)이고, 심볼 임베딩은 레포·커밋·모델마다 캐시된다(없으면 첫 실행에 생성 비용).

실행:
  uv run python evals/experiments/issue_matcher_eval.py --dry-run   # 이슈·레포·예상 쿼리 수만
  uv run python evals/experiments/issue_matcher_eval.py             # 임베딩 쿼리 20회
  uv run python evals/experiments/issue_matcher_eval.py --ids im-rich-01,im-rich-02 --k-symbols 10

임베딩 API 는 토큰 콜백(get_usage_metadata_callback)에 잡히지 않으므로 토큰은 글자 수 / 4 로
추정해 "추정" 으로 표시한다 (실제 값은 제공자 대시보드에서 확인).
"""

import argparse
import importlib.util
import json
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from repoute.github.issues import Issue, parse_issues
from repoute.graphs.issue_matcher import MAX_MODULES, QUERY_BODY, SEARCH_K, _module_names

DATASET = "evals/datasets/issue_matcher_gold.json"
RAW_DIR = "evals/datasets/raw"
DEFAULT_OUT = "evals/results/25-issue-matcher-gold.json"
MAX_SYMBOL_NAMES = 5  # 심볼 적중을 볼 예측 심볼 이름 수
MODULE_K = 3  # p@k·hit@k 의 k (issue_matcher 의 MAX_MODULES 와 같다)
DIFFICULTIES = ("easy", "medium", "hard")
# 임베딩 단가(USD / 1M 토큰). 스크립트는 패키지가 아니라 복사해 둔다.
EMBED_PRICES = {"text-embedding-3-small": 0.02, "text-embedding-3-large": 0.13}
METRICS = Path(__file__).resolve().parents[1] / "evaluators" / "metrics.py"


def _load_metrics():
    """evals/ 는 패키지가 아니라 metrics.py 를 파일 경로로 읽는다 (다른 실험 스크립트와 같다)."""
    spec = importlib.util.spec_from_file_location("metrics", METRICS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = _load_metrics()


# ---- 이슈 텍스트 (네트워크 없음) ----


def raw_files(raw_dir: Path | str, repo: str) -> list[Path]:
    """레포 "owner/name" 의 raw 파일 목록 (issues_o__n.json, issues_o__n_page2.json …)."""
    owner, _, name = repo.partition("/")
    stem = f"issues_{owner}__{name}"
    # glob 만 쓰면 "o/r" 이 issues_o__rich.json 까지 잡으므로 이름 뒤가 "_" 일 때만 같은 레포
    return sorted(
        p
        for p in Path(raw_dir).glob(f"{stem}*.json")
        if p.stem == stem or p.stem.startswith(stem + "_")
    )


def find_issue(raw_dir: Path | str, repo: str, number: int) -> Issue | None:
    """raw 파일들에서 번호가 같은 이슈를 찾는다. parse_issues 를 거쳐 그래프와 같은 Issue 를 만든다.

    parse_issues 가 PR 을 빼고 본문을 BODY_LIMIT 자로 자르므로, MCP 로 받은 이슈와 같은 모양이다.
    여러 파일에 있으면 처음 찾은 것, 없으면 None.
    """
    for path in raw_files(raw_dir, repo):
        for issue in parse_issues(json.loads(path.read_text(encoding="utf-8"))):
            if issue.number == number:
                return issue
    return None


def query_text(issue: Issue) -> str:
    """임베딩 쿼리 텍스트: 제목 + 줄바꿈 + 본문 앞 QUERY_BODY 자 (match_symbols 와 같은 구성)."""
    return issue.title + "\n" + issue.body[:QUERY_BODY]


def estimate_tokens(text: str) -> int:
    """토큰 추정: 글자 수 / 4 (embeddings.py 로그와 같은 근사)."""
    return len(text) // 4


def embed_cost(tokens: int, model: str) -> float | None:
    """임베딩 비용(USD). "openai:..." 처럼 제공자가 붙어도 된다. 단가를 모르면 None."""
    price = EMBED_PRICES.get(model.split(":", 1)[-1])
    return None if price is None else tokens * price / 1_000_000


# ---- 매칭·지표 ----


def predict(index, store, embedder, issue: Issue, k_symbols: int = SEARCH_K) -> dict:
    """이슈 하나를 임베딩 검색해 예측 모듈(MAX_MODULES 개)·심볼 이름(MAX_SYMBOL_NAMES 개)을 만든다.

    match_symbols 와 같은 순서: embed_query → store.search(k) → 상위 심볼 파일을 _module_names 로
    모듈 이름으로 바꿔 중복 제거 후 앞 MAX_MODULES 개.
    """
    text = query_text(issue)
    start = time.perf_counter()
    hits = store.search(embedder.embed_query(text), k=k_symbols)
    deps = SimpleNamespace(index=index)  # _module_names 는 deps.index.graph 만 본다
    modules = _module_names(deps, [h["file"] for h in hits])[:MAX_MODULES]
    return {
        "modules": modules,
        "symbols": [h["name"] for h in hits[:MAX_SYMBOL_NAMES]],
        "hits": [
            {"name": h["name"], "file": h["file"], "line": h["line_start"], "score": h["score"]}
            for h in hits
        ],
        "query_chars": len(text),
        "query_tokens_est": estimate_tokens(text),
        "seconds": round(time.perf_counter() - start, 3),
    }


def score(predicted_modules: list[str], predicted_symbols: list[str], item: dict) -> dict:
    """gold 한 항목에 대한 지표 dict: p@3, hit@3, 심볼 hit, 1위 모듈 적중.

    1위 적중은 predicted[0] 이 gold 에 있는가를 hit@1 로 잰다 (모듈 비교 규칙이 다른 지표와 같게).
    """
    gold_modules = item["gold_modules"]
    gold_symbols = [s["name"] for s in item["gold_symbols"]]
    return {
        "precision_at_3": m.issue_module_precision_at_k(predicted_modules, gold_modules, MODULE_K),
        "hit_at_3": m.issue_module_hit_at_k(predicted_modules, gold_modules, MODULE_K),
        "symbol_hit": m.issue_symbol_hit(predicted_symbols, gold_symbols),
        "top1_hit": m.issue_module_hit_at_k(predicted_modules, gold_modules, 1),
    }


def evaluate_item(index, store, embedder, item: dict, issue: Issue, k_symbols: int) -> dict:
    """gold 한 항목: 예측 → 지표. 결과 행 dict (표·집계 입력)."""
    pred = predict(index, store, embedder, issue, k_symbols)
    return {
        "id": item["id"],
        "repo": item["repo"],
        "number": item["number"],
        "title": issue.title,
        "difficulty": item["difficulty"],
        "gold_modules": item["gold_modules"],
        "gold_symbols": [s["name"] for s in item["gold_symbols"]],
        **pred,
        "metrics": score(pred["modules"], pred["symbols"], item),
    }


def _rate(values: list) -> float | None:
    """평균 (bool 은 비율). 빈 목록이면 None."""
    return sum(values) / len(values) if values else None


def summarize(rows: list[dict]) -> dict:
    """행 목록의 요약: n·지표 평균·추정 토큰 합. 오류 행(error 키)은 n 에서 빼고 failed 로 센다."""
    ok = [r for r in rows if "error" not in r]
    metric = [r["metrics"] for r in ok]
    return {
        "n": len(ok),
        "failed": len(rows) - len(ok),
        "precision_at_3": _rate([x["precision_at_3"] for x in metric]),
        "hit_at_3": _rate([x["hit_at_3"] for x in metric]),
        "symbol_hit": _rate([x["symbol_hit"] for x in metric]),
        "top1_hit": _rate([x["top1_hit"] for x in metric]),
        "query_tokens_est": sum(r["query_tokens_est"] for r in ok),
    }


def aggregate(rows: list[dict]) -> dict:
    """이슈별 행 → {"by_repo": {레포: 요약}, "by_difficulty": {난이도: 요약}, "overall": 요약}.

    레포는 처음 나온 순서, 난이도는 easy·medium·hard 순서이고 행이 없는 난이도는 뺀다.
    """
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


def _ox(x: bool) -> str:
    return "O" if x else "X"


def _cell(text: str) -> str:
    """마크다운 표 칸에서 '|' 가 칸을 나누지 않게."""
    return text.replace("|", "\\|")


def render_issue_table(rows: list[dict]) -> str:
    """이슈별 표: id, 번호, 제목 30자, gold 모듈, 예측 모듈 3, p@3, hit, 심볼 hit, 1위."""
    lines = [
        "| id | # | 제목 | gold 모듈 | 예측 모듈 | p@3 | hit@3 | 심볼 hit | 1위 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        title = _cell(r.get("title", "")[:30])
        gold = ", ".join(r["gold_modules"])
        if "error" in r:
            lines.append(f"| {r['id']} | {r['number']} | {title} | {gold} | {r['error']} |||||")
            continue
        x = r["metrics"]
        lines.append(
            f"| {r['id']} | {r['number']} | {title} | {gold} | {', '.join(r['modules'])} "
            f"| {x['precision_at_3']:.2f} | {_ox(x['hit_at_3'])} | {_ox(x['symbol_hit'])} "
            f"| {_ox(x['top1_hit'])} |"
        )
    return "\n".join(lines)


def render_summary_table(label: str, groups: dict[str, dict], model: str) -> str:
    """요약 표 하나 (레포별·난이도별·전체). 토큰·비용은 글자 수 / 4 추정."""
    lines = [
        f"| {label} | n | p@3 | hit@3 | 심볼 hit | 1위 | 쿼리 토큰(추정) | 비용(추정) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for key, s in groups.items():
        cost = embed_cost(s["query_tokens_est"], model)
        n = f"{s['n']}" + (f" (실패 {s['failed']})" if s["failed"] else "")
        lines.append(
            f"| {key} | {n} | {_pct(s['precision_at_3'])} | {_pct(s['hit_at_3'])} "
            f"| {_pct(s['symbol_hit'])} | {_pct(s['top1_hit'])} | ~{s['query_tokens_est']} "
            f"| {'—' if cost is None else f'~${cost:.6f}'} |"
        )
    return "\n".join(lines)


# ---- 실행 ----


def select_items(items: list[dict], ids: str | None) -> list[dict]:
    """--ids(쉼표 구분)가 있으면 그 id 만, 순서는 데이터셋 순서. 모르는 id 면 ValueError."""
    if not ids:
        return items
    wanted = [i.strip() for i in ids.split(",") if i.strip()]
    unknown = set(wanted) - {item["id"] for item in items}
    if unknown:
        raise ValueError(f"데이터셋에 없는 id: {', '.join(sorted(unknown))}")
    return [item for item in items if item["id"] in wanted]


def embedding_cached(cache_dir: Path | str, repo: str) -> bool:
    """심볼 임베딩 캐시 파일이 있는가 (인덱스를 열지 않고 파일 이름만 본다, 커밋은 모름)."""
    owner, _, name = repo.partition("/")
    return any((Path(cache_dir) / "index").glob(f"{owner}__{name}@*.emb.sqlite"))


def dry_run(targets: list[tuple[dict, Issue | None]], cache_dir: str, model: str) -> None:
    """이슈 수·레포·예상 임베딩 쿼리 수·추정 토큰·비용만 출력한다 (인덱스·API 호출 없음)."""
    found = [(item, issue) for item, issue in targets if issue is not None]
    tokens = sum(estimate_tokens(query_text(issue)) for _, issue in found)
    cost = embed_cost(tokens, model)
    repos = dict.fromkeys(item["repo"] for item, _ in targets)
    print(f"이슈 {len(targets)}개 (raw 에서 찾음 {len(found)}개), 레포 {len(repos)}개")
    for repo in repos:
        n = sum(item["repo"] == repo for item, _ in targets)
        cached = (
            "있음" if embedding_cached(cache_dir, repo) else "없음 → 첫 실행에 심볼 임베딩 비용"
        )
        print(f"- {repo}: 이슈 {n}개, 심볼 임베딩 캐시 {cached}")
    for item, issue in targets:
        if issue is None:
            print(f"  [없음] {item['id']} #{item['number']} — raw 파일에 이 번호가 없음")
    print(f"예상 임베딩 쿼리 수: {len(found)} (모델 {model})")
    print(
        f"쿼리 토큰(추정, 글자 수/4): ~{tokens}, 비용(추정): "
        + ("—" if cost is None else f"~${cost:.6f}")
    )


def _error_row(item: dict) -> dict:
    """실패한 gold 항목의 행 뼈대 (표에 id·번호·gold 는 보이게)."""
    return {
        "id": item["id"],
        "repo": item["repo"],
        "number": item["number"],
        "title": item.get("title", ""),
        "difficulty": item["difficulty"],
        "gold_modules": item["gold_modules"],
    }


def main() -> None:
    """gold 이슈마다 임베딩 매칭 → 지표, .json(raw) 저장, 이슈별·요약 표를 출력한다."""
    parser = argparse.ArgumentParser(description="Issue Matcher 매칭 단계(RAG) 평가 (임베딩만)")
    parser.add_argument("--dataset", default=DATASET, help="issue_matcher_gold.json")
    parser.add_argument("--raw-dir", default=RAW_DIR, help="issues_<owner>__<name>*.json 폴더")
    parser.add_argument("--k-symbols", type=int, default=SEARCH_K, help="이슈마다 찾을 심볼 수")
    parser.add_argument("--ids", default=None, help="쉼표로 구분한 gold id (기본 전체)")
    parser.add_argument("--cache-dir", default=".repoute_cache", help="clone·인덱스·임베딩 캐시")
    parser.add_argument("--out", default=DEFAULT_OUT, help="raw 결과 json")
    parser.add_argument("--dry-run", action="store_true", help="대상·예상 쿼리 수·비용만 출력")
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv(".env")  # troubleshooting 06: 경로를 명시해야 키가 읽힌다
    from repoute.config import get_embedder, load_settings

    settings = load_settings()
    items = select_items(json.loads(Path(args.dataset).read_text(encoding="utf-8")), args.ids)
    targets = [(item, find_issue(args.raw_dir, item["repo"], item["number"])) for item in items]

    if args.dry_run:
        dry_run(targets, args.cache_dir, settings.embedding_model)
        return

    from repoute.indexing.embeddings import ensure_embeddings
    from repoute.indexing.index import build_index

    embedder = get_embedder(settings)
    rows, repo_meta = [], {}
    for repo in dict.fromkeys(item["repo"] for item, _ in targets):
        index = build_index(f"https://github.com/{repo}", args.cache_dir)
        try:
            store = ensure_embeddings(index, embedder, settings.embedding_model, args.cache_dir)
            repo_meta[repo] = {
                "repo_id": index.info.repo_id,
                "symbols_embedded": store.count(),
                "embedding_cached": store.cached,
            }
            print(
                f"[index] {index.info.repo_id}: 심볼 임베딩 {store.count()}개"
                + (" (캐시)" if store.cached else " (새로 만듦 — 임베딩 비용 발생)")
            )
            for item, issue in targets:
                if item["repo"] != repo:
                    continue
                if issue is None:
                    rows.append({**_error_row(item), "error": "raw 에 없음"})
                    continue
                try:
                    rows.append(evaluate_item(index, store, embedder, item, issue, args.k_symbols))
                except Exception as e:  # 한 이슈가 실패해도 나머지는 계속 잰다
                    text = str(e).strip()
                    message = text.splitlines()[0] if text else ""
                    rows.append({**_error_row(item), "error": f"{type(e).__name__}: {message}"})
        finally:
            index.db.close()

    summary = aggregate(rows)
    total_tokens = summary["overall"]["query_tokens_est"]
    result = {
        "meta": {
            "date": date.today().isoformat(),
            "dataset": args.dataset,
            "raw_dir": args.raw_dir,
            "embedding_model": settings.embedding_model,
            "k_symbols": args.k_symbols,
            "module_k": MODULE_K,
            "max_symbol_names": MAX_SYMBOL_NAMES,
            "query": f'title + "\\n" + body[:{QUERY_BODY}] (issue_matcher.match_symbols 와 같음)',
            "query_tokens_est": total_tokens,
            "cost_est": embed_cost(total_tokens, settings.embedding_model),
            "token_note": "임베딩 API 는 토큰 콜백에 안 잡혀 글자 수/4 추정",
            "repos": repo_meta,
        },
        "rows": rows,
        "summary": summary,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    model = settings.embedding_model
    print()
    print(render_issue_table(rows))
    print()
    print(render_summary_table("레포", summary["by_repo"], model))
    print()
    print(render_summary_table("난이도", summary["by_difficulty"], model))
    print()
    print(render_summary_table("전체", {"전체": summary["overall"]}, model))
    print()
    print(f"- 예측 모듈 상위 {MAX_MODULES}개는 심볼 {args.k_symbols}개의 파일에서 만든다.")
    print(f"- 심볼 hit 는 예측 심볼 이름 상위 {MAX_SYMBOL_NAMES}개와 gold 끝 이름 비교.")
    print("- 토큰·비용은 추정 (임베딩 API 는 토큰 콜백에 안 잡힘, 글자 수/4).")
    print(f"저장: {out}")


if __name__ == "__main__":
    main()
