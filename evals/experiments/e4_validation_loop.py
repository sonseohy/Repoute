"""실험 E4: 인용 검증 루프 유무 비교 (naive vs validate). 실제 LLM 을 호출한다.

실행: uv run python evals/experiments/e4_validation_loop.py --mode both
"""

import argparse
import importlib
import json
import time
from datetime import date
from pathlib import Path

from langchain_core.callbacks import UsageMetadataCallbackHandler

from repoute.config import get_fast_llm, get_quality_llm, load_settings
from repoute.graphs.deps import Deps
from repoute.graphs.reading_guide import MAX_RETRIES, build_reading_guide_graph
from repoute.indexing.index import build_index
from repoute.validation.citations import citation_validity, validate_citations

REPOS = ["psf/requests", "encode/httpx", "fastapi/fastapi", "pallets/click", "Textualize/rich"]
# 2026-10 OpenAI 공시 단가(USD / 1M 토큰). 바뀌면 여기만 고친다. LangSmith 의 비용과 교차 확인용.
PRICES = {"gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.00)}


def _price(model: str) -> tuple[float, float] | None:
    """모델 단가. OpenAI 는 'gpt-4o-mini-2024-07-18' 처럼 날짜를 붙여서 최장 접두어로 찾는다."""
    keys = [k for k in PRICES if model == k or model.startswith(k + "-")]
    return PRICES[max(keys, key=len)] if keys else None


def estimate_cost(usage: dict) -> float | None:
    """{모델: {"input_tokens", "output_tokens"}} 의 비용(USD). 단가 모르는 모델·빈 값이면 None."""
    if not usage:
        return None  # 토큰을 못 쟀으면 0 이 아니라 '모름'
    total = 0.0
    for model, u in usage.items():
        price = _price(model)
        if price is None:
            return None
        total += (u["input_tokens"] * price[0] + u["output_tokens"] * price[1]) / 1_000_000
    return total


def run_one(repo: str, validate: bool, cache_dir, prompt: str = "v1") -> dict:
    """레포 하나를 한 모드·프롬프트로 돌리고, 결과 인용을 사후 검증해 지표 dict 를 돌려준다."""
    prompts = importlib.import_module(f"repoute.prompts.reading_guide_{prompt}")
    index = build_index(f"https://github.com/{repo}", cache_dir)
    try:
        deps = Deps(get_fast_llm(), get_quality_llm(), index)
        graph = build_reading_guide_graph(deps, validate, prompts=prompts)
        mode = "validate" if validate else "naive"
        cb = UsageMetadataCallbackHandler()  # 그래프 안 모든 LLM 호출의 토큰을 모델별로 합산
        config = {"callbacks": [cb], "tags": ["e4", mode, repo, f"prompt-{prompt}"]}
        start = time.perf_counter()
        state = graph.invoke({"retry_count": 0, "validation_errors": []}, config=config)
        elapsed = time.perf_counter() - start
        # naive 도 같은 기준으로 채점: 그래프 상태가 아니라 최종 guide 를 다시 검증한다
        guide = state["guide"]
        refs = guide.entrypoints + [step.ref for step in guide.reading_order]
        root = index.info.path
        tokens = {
            m: {"input_tokens": u["input_tokens"], "output_tokens": u["output_tokens"]}
            for m, u in cb.usage_metadata.items()
        }
        return {
            "repo": index.info.repo_id,
            "mode": mode,
            "prompt": prompt,
            "files": index.file_count,
            "targets": len(state.get("targets", [])),
            "citations": len(refs),
            "refs": [f"{r.file}:{r.line}" for r in refs],  # 어떤 인용을 냈는지 (사후 분석용)
            "valid": citation_validity(refs, index.db, root),
            "errors": len(validate_citations(refs, index.db, root)),
            "error_messages": validate_citations(refs, index.db, root),  # 무엇이 틀렸는지
            "retries": state.get("retry_count", 0),
            "tokens": tokens,
            "cost": estimate_cost(tokens),
            "elapsed": elapsed,
        }
    finally:
        index.db.close()


def _money(x: float | None) -> str:
    return "—" if x is None else f"${x:.4f}"


def _avg(values: list) -> float | None:
    """평균. 모르는 값(None)이 하나라도 있으면 None (측정 안 한 수치 금지)."""
    return None if not values or None in values else sum(values) / len(values)


DEFAULT_TITLE = "03. 실험 E4 — 인용 검증 루프 유무"


def render_markdown(rows: list[dict], title: str = DEFAULT_TITLE) -> str:
    """raw 결과를 실험 기록 마크다운으로 만든다. '관찰' 은 사람이 채운다."""
    s = load_settings()
    ok = [r for r in rows if "error" not in r]
    n = len({r["repo"] for r in ok})
    # 예전 결과(03 기록)에는 prompt 키가 없다 — 그때는 v1 만 있었다
    versions = ", ".join(sorted({r.get("prompt", "v1") for r in rows}))
    lines = [
        f"# {title} (Phase 2, {date.today().isoformat()})",
        "",
        "## 방법",
        "",
        f"- **n = {n}** 레포, 모드 2개(naive=`validate=False`, validate=검증 루프, "
        f"재생성 상한 {MAX_RETRIES}회)",
        f"- **모델**: fast = {s.fast_model}, quality = {s.quality_model} ({s.provider}), "
        f"temperature 0. 프롬프트 {versions}.",
        "- **합성 입력**: 모듈마다 `인용 가능한 줄: 1(모듈 시작), 이름:줄, ...` 목록과 "
        "검증 오류별 힌트(`→ 인용 가능한 줄`·`비슷한 경로`)를 넣는다. 03 기록(E4) 이후 "
        "v1·v2 공통으로 바뀐 입력이라, 03 의 v1 결과와는 프롬프트 외에 이 점도 다르다.",
        "- **인용 유효성**: 두 모드 모두 최종 guide 의 인용(진입점 + 각 단계 ref)을 "
        "`citation_validity`·`validate_citations` 로 사후 검증 (naive 는 루프 없이 결과만 채점).",
        "- **토큰**: `UsageMetadataCallbackHandler` 로 그래프 안 LLM 호출을 모델별 합산.",
        "- **비용**: 스크립트의 `PRICES` 단가로 계산한 추정치. LangSmith(태그 `e4`)의 비용과 "
        "교차 확인 가능.",
        "- **시간**: 그래프 `invoke` 의 벽시계 시간(perf_counter). 인덱싱·clone 제외.",
        "",
        "## 레포별 결과",
        "",
        "| 레포 | 모드 | files | targets | 인용 수 | 유효 비율 | 오류 수 | 재생성 "
        "| in/out 토큰 | 비용 | 시간 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in ok:
        t_in = sum(u["input_tokens"] for u in r["tokens"].values())
        t_out = sum(u["output_tokens"] for u in r["tokens"].values())
        lines.append(
            f"| {r['repo']} | {r['mode']} | {r['files']} | {r['targets']} | {r['citations']} "
            f"| {r['valid']:.0%} | {r['errors']} | {r['retries']} | {t_in:,} / {t_out:,} "
            f"| {_money(r['cost'])} | {r['elapsed']:.1f}s |"
        )
    lines += [
        "",
        "## 모드별 평균",
        "",
        "| 모드 | n | 유효 비율 평균 | 오류 합 | 비용 평균 | 시간 평균 |",
        "|---|---|---|---|---|---|",
    ]
    for mode in ("naive", "validate"):
        group = [r for r in ok if r["mode"] == mode]
        if not group:
            continue
        valid = _avg([r["valid"] for r in group])
        lines.append(
            f"| {mode} | n={len(group)} | {valid:.0%} | {sum(r['errors'] for r in group)} "
            f"| {_money(_avg([r['cost'] for r in group]))} "
            f"| {_avg([r['elapsed'] for r in group]):.1f}s |"
        )
    failed = [r for r in rows if "error" in r]
    if failed:
        lines += ["", "## 실패", ""] + [
            f"- {r['repo']} ({r['mode']}): {r['error']}" for r in failed
        ]
    lines += ["", "## 관찰", "", "- ", "- ", ""]
    return "\n".join(lines)


def main() -> None:
    """레포마다 naive → validate 순으로 돌리고 .json(raw)·.md(요약)로 저장한다."""
    parser = argparse.ArgumentParser(description="실험 E4: 인용 검증 루프 유무 (LLM 호출)")
    parser.add_argument("--repos", nargs="+", default=REPOS, help="owner/name 목록")
    parser.add_argument("--mode", choices=["naive", "validate", "both"], default="both")
    parser.add_argument("--cache-dir", default=".repoute_cache", help="clone·인덱스 캐시")
    parser.add_argument("--prompt", choices=["v1", "v2"], default="v1", help="프롬프트 버전")
    parser.add_argument("--out", default="evals/results/03-e4-validation-loop.md")
    parser.add_argument("--title", default=DEFAULT_TITLE, help="결과 마크다운 제목 (번호. 실험명)")
    args = parser.parse_args()
    modes = {"naive": [False], "validate": [True], "both": [False, True]}[args.mode]

    rows = []
    for repo in args.repos:
        for validate in modes:
            mode = "validate" if validate else "naive"
            try:
                row = run_one(repo, validate, args.cache_dir, args.prompt)
                print(
                    f"[ok] {repo} {mode}: 유효 {row['valid']:.0%}, 재생성 {row['retries']}, "
                    f"{_money(row['cost'])}, {row['elapsed']:.1f}s"
                )
            except Exception as e:  # 한 레포가 실패해도 나머지는 계속 잰다
                message = str(e).strip().splitlines()[0] if str(e).strip() else ""
                error = f"{type(e).__name__}: {message}"
                row = {"repo": repo, "mode": mode, "prompt": args.prompt, "error": error}
                print(f"[fail] {repo} {mode}: {row['error']}")
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
