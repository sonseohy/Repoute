"""/explain 품질 측정: 설명 프롬프트 v1·v2 를 같은 심볼에 돌려 watch 지표 비교. 실제 LLM 호출.

대상 심볼은 walkthrough_questions.json 의 chain_evidence 앞부분 "path:line" 에서 뽑는다
(레포별 5개, 최대 20개). 자동 지표는 LLM 없이 계산하고, --judge 면 fast 모델이
"watch 첫 항목이 핵심 로직 줄인가" 를 판정한다.

실행:
  uv run python evals/experiments/explain_quality.py --dry-run   # 대상·예상 호출 수만
  uv run python evals/experiments/explain_quality.py             # v1·v2, 판정 끔
  uv run python evals/experiments/explain_quality.py --prompts v2 --judge --no-cache

판정은 기본 끔 (기록 22: 판정 모델의 변별력이 0 이었다). watch 는 explain_symbol 이
normalize_watch 로 후처리한 결과만 잰다 (meta.postprocess).
"""

import argparse
import ast
import importlib
import json
import re
from datetime import date
from pathlib import Path

from langchain_core.callbacks import UsageMetadataCallbackHandler, get_usage_metadata_callback
from pydantic import BaseModel, Field

from repoute.config import get_fast_llm, load_settings
from repoute.indexing.explain import (
    SymbolSpan,
    _numbered_code,
    enclosing_symbol,
    explain_symbol,
)
from repoute.indexing.index import build_index

DATASET = "evals/datasets/walkthrough_questions.json"
DEFAULT_CACHE = ".repoute_cache/evals/explain_quality.sqlite"
DEFAULT_OUT = "evals/results/22-ui-baselines-explain.json"
PER_REPO = 5
MAX_SYMBOLS = 20
# walkthrough_batch.py 와 같은 단가(USD / 1M 토큰). 스크립트는 패키지가 아니라 복사해 둔다.
PRICES = {"gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.00)}

# chain_evidence 앞부분 "src/requests/api.py:87·71 ..." 에서 파일과 첫 줄 번호
EVIDENCE_RE = re.compile(r"^\s*([\w./-]+\.py):(\d+)")
# watch 항목의 "(줄 N)" 또는 "(줄 N~M)"
LINE_REF_RE = re.compile(r"\(줄\s*(\d+)(?:\s*[~\-–]\s*(\d+))?\)")

JUDGE = """당신은 코드 리뷰어입니다. 아래 {kind} 코드와, 이 코드에서 "꼭 봐야 할 코드" 로
뽑힌 첫 항목이 있습니다.

이름: {name}
파일: {file}

코드 (줄 번호 포함):
{code}

첫 항목: {first}

질문: 첫 항목이 가리키는 줄이 이 {kind} 의 결과를 실제로 만드는 핵심 로직 줄입니까?
- 핵심 로직 줄: 반환값·부수효과(상태 변경, 다른 핵심 함수 호출로 결과 생성)를 실제로 만드는 줄.
- 핵심이 아닌 줄: def/class 선언, docstring, 단순 인자 검사·기본값 설정, import, 로그 등.
core_logic 에 true/false, reason 에 한국어 한 문장 근거를 쓰세요.
"""

JUDGE_LIMIT = (
    "판정 모델(fast)이 설명 모델과 같은 계열이라 같은 편향을 공유할 수 있다 "
    "(자기 평가 경향). 판정은 사람 판정이 아닌 보조 지표로만 본다."
)


class Judge(BaseModel):
    """watch 첫 항목이 핵심 로직 줄을 가리키는지에 대한 판정."""

    core_logic: bool = Field(description="첫 항목이 결과를 실제로 만드는 핵심 로직 줄이면 true")
    reason: str = Field(description="판정 근거, 한국어 한 문장")


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


# ---- 대상 심볼 고르기 (LLM 없음) ----


def parse_evidence(text: str) -> tuple[str, int] | None:
    """chain_evidence 한 줄의 앞부분 "path:line" 을 (파일, 줄)로. 형식이 아니면 None.

    "src/requests/api.py:87·71 ..." 처럼 줄이 여러 개면 첫 줄만 쓴다.
    """
    m = EVIDENCE_RE.match(text)
    return (m.group(1), int(m.group(2))) if m else None


def evidence_locations(items: list[dict]) -> dict[str, list[tuple[str, int]]]:
    """질문들의 chain_evidence 에서 레포별 (파일, 줄) 목록. 순서를 지키며 중복을 뺀다."""
    out: dict[str, list[tuple[str, int]]] = {}
    for item in items:
        for text in item.get("chain_evidence", []):
            loc = parse_evidence(text)
            locs = out.setdefault(item["repo"], [])
            if loc is not None and loc not in locs:
                locs.append(loc)
    return out


def select_symbols(index, locs: list[tuple[str, int]], per_repo: int = PER_REPO):
    """위치를 차례로 감싸는 심볼로 바꿔 per_repo 개까지. 반환 (심볼 목록, 건너뛴 기록).

    감싸는 심볼이 없거나(None) 앞 위치와 같은 심볼이면 건너뛰고 이유를 남긴다
    (같은 심볼을 두 번 재면 캐시라 지표만 중복된다).
    """
    root, picked, skipped, seen = index.info.path, [], [], set()
    for file, line in locs:
        if len(picked) >= per_repo:
            break
        span = enclosing_symbol(index.db, root, file, line)
        where = {"repo": index.info.repo_id, "file": file, "line": line}
        if span is None:
            skipped.append({**where, "reason": "감싸는 심볼 없음"})
        elif (span.file, span.line_start) in seen:
            skipped.append({**where, "reason": f"중복 심볼 {span.name}"})
        else:
            seen.add((span.file, span.line_start))
            picked.append((line, span))
    return picked, skipped


# ---- 자동 지표 (LLM 없음) ----


def line_refs(item: str) -> list[tuple[int, int]]:
    """watch 항목 하나의 줄 참조 목록 [(시작, 끝)]. "(줄 N)" 은 (N, N)."""
    return [(int(a), int(b or a)) for a, b in LINE_REF_RE.findall(item)]


def ref_validity(watch: list[str], span: SymbolSpan) -> float | None:
    """줄 참조가 모두 심볼 범위 [line_start, line_end] 안인 항목 비율. 참조 없는 항목은 무효.

    watch 가 비어 있으면 None (잴 것이 없음).
    """
    if not watch:
        return None
    ok = 0
    for item in watch:
        refs = line_refs(item)
        if refs and all(span.line_start <= a <= b <= span.line_end for a, b in refs):
            ok += 1
    return ok / len(watch)


def has_ref_rate(watch: list[str]) -> float | None:
    """줄 참조가 하나라도 있는 항목 비율. watch 가 비어 있으면 None."""
    if not watch:
        return None
    return sum(bool(line_refs(item)) for item in watch) / len(watch)


def count_ok(watch: list[str]) -> bool:
    """watch 가 프롬프트 규칙대로 2~4개인가."""
    return 2 <= len(watch) <= 4


def docstring_ko_ok(span: SymbolSpan, docstring_ko: str) -> bool:
    """원문 docstring 이 있으면 docstring_ko 가 비어 있지 않고, 없으면 비어 있어야 한다."""
    return bool(docstring_ko.strip()) == bool(span.docstring)


def docstring_end(source: str, line_start: int) -> int | None:
    """source 에서 line_start 에 정의된 함수·클래스의 docstring 끝 줄. 없거나 파싱 실패면 None."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(tree):
        if isinstance(node, kinds) and node.lineno == line_start:
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                return first.end_lineno
            return None
    return None


def first_is_def_or_doc(watch: list[str], span: SymbolSpan, lines: list[str]) -> bool | None:
    """watch 첫 항목의 시작 줄이 def/class 줄이거나 docstring 범위(심볼 시작~docstring 끝)인가.

    "핵심 로직이 아닌 줄" 의 보수적 자동 근사다 (True 면 확실히 핵심 로직이 아님).
    lines 는 파일 전체의 read_text().splitlines(). 첫 항목이 없거나 줄 참조가 없으면 None.
    """
    if not watch or not (refs := line_refs(watch[0])):
        return None
    start = refs[0][0]
    if 1 <= start <= len(lines):
        text = lines[start - 1].lstrip()
        if text.startswith(("def ", "async def ", "class ")):
            return True
    doc_end = docstring_end("\n".join(lines), span.line_start)
    return doc_end is not None and span.line_start <= start <= doc_end


def metrics(expl, span: SymbolSpan, lines: list[str]) -> dict:
    """설명 하나의 자동 지표 dict."""
    return {
        "ref_validity": ref_validity(expl.watch, span),
        "has_ref_rate": has_ref_rate(expl.watch),
        "count_ok": count_ok(expl.watch),
        "docstring_ko_ok": docstring_ko_ok(span, expl.docstring_ko),
        "first_is_def_or_doc": first_is_def_or_doc(expl.watch, span, lines),
    }


# ---- LLM 호출 ----


def _tokens(cb: UsageMetadataCallbackHandler) -> dict:
    """콜백이 모은 모델별 입력·출력 토큰."""
    return {
        m: {"input_tokens": u["input_tokens"], "output_tokens": u["output_tokens"]}
        for m, u in cb.usage_metadata.items()
    }


def judge_first(llm, root: Path, span: SymbolSpan, first: str) -> tuple[Judge, dict]:
    """watch 첫 항목이 핵심 로직 줄인지 fast 모델로 판정. 반환 (판정, 토큰)."""
    code, _ = _numbered_code(root, span)
    prompt = JUDGE.format(name=span.name, kind=span.kind, file=span.file, code=code, first=first)
    with get_usage_metadata_callback() as cb:
        verdict = llm.with_structured_output(Judge).invoke(prompt)
    return verdict, _tokens(cb)


def run_one(llm, index, line: int, span: SymbolSpan, prompts, cache_path: Path, judge: bool):
    """(심볼, 프롬프트) 하나: 설명 → 자동 지표 → (선택) 판정. 결과 dict."""
    root = index.info.path
    with get_usage_metadata_callback() as cb:  # 캐시 적중이면 토큰 0
        expl, cached = explain_symbol(
            llm, root, span, cache_path, index.info.repo_id, prompts=prompts
        )
    explain_tokens = _tokens(cb)
    lines = (root / span.file).read_text(encoding="utf-8", errors="replace").splitlines()
    verdict, judge_tokens = None, {}
    if judge and expl.watch:
        verdict, judge_tokens = judge_first(llm, root, span, expl.watch[0])
    # 캐시 적중은 이번 실행 비용 0, 판정을 안 했으면 판정 비용 0
    explain_cost = 0.0 if cached else estimate_cost(explain_tokens)
    judge_cost = estimate_cost(judge_tokens) if judge_tokens else 0.0
    cost = None if None in (explain_cost, judge_cost) else explain_cost + judge_cost
    return {
        "repo": index.info.repo_id,
        "evidence_line": line,
        "symbol": span.name,
        "kind": span.kind,
        "file": span.file,
        "line_start": span.line_start,
        "line_end": span.line_end,
        "has_docstring": bool(span.docstring),
        "prompt": prompts.VERSION,
        "cached": cached,
        "explanation": expl.model_dump(),
        "metrics": metrics(expl, span, lines),
        "judge": verdict.model_dump() if verdict else None,
        "tokens": {"explain": explain_tokens, "judge": judge_tokens},
        "cost": cost,
    }


# ---- 표 ----


def _mean(values: list) -> float | None:
    """None 을 뺀 평균. 남는 값이 없으면 None."""
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.0%}"


def _money(x: float | None) -> str:
    return "—" if x is None else f"${x:.4f}"


def _ratio(values: list) -> str:
    """bool 목록(None 은 뺌)의 'k/n (비율)'."""
    values = [v for v in values if v is not None]
    if not values:
        return "—"
    hit = sum(values)
    return f"{hit}/{len(values)} ({hit / len(values):.0%})"


def render_table(rows: list[dict]) -> str:
    """프롬프트별 요약 마크다운 표. 오류 행은 n 에서 뺀다."""
    lines = [
        "| 프롬프트 | n | ref_validity | has_ref_rate | count_ok | docstring_ko_ok "
        "| first_is_def_or_doc | judge core_logic | 비용 합 | 비용 평균 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    ok = [r for r in rows if "error" not in r]
    for version in dict.fromkeys(r["prompt"] for r in ok):
        group = [r for r in ok if r["prompt"] == version]
        m = [r["metrics"] for r in group]
        costs = [r["cost"] for r in group]
        total = None if None in costs else sum(costs)
        judged = [r["judge"]["core_logic"] if r["judge"] else None for r in group]
        lines.append(
            f"| {version} | {len(group)} | {_pct(_mean([x['ref_validity'] for x in m]))} "
            f"| {_pct(_mean([x['has_ref_rate'] for x in m]))} "
            f"| {_ratio([x['count_ok'] for x in m])} "
            f"| {_ratio([x['docstring_ko_ok'] for x in m])} "
            f"| {_ratio([x['first_is_def_or_doc'] for x in m])} "
            f"| {_ratio(judged)} | {_money(total)} "
            f"| {_money(None if total is None else total / len(group))} |"
        )
    return "\n".join(lines)


def main() -> None:
    """대상 심볼 × 프롬프트마다 설명·지표·판정을 재고 .json(raw) 저장, 표를 출력한다."""
    parser = argparse.ArgumentParser(description="/explain 품질 측정 (LLM 호출)")
    parser.add_argument("--dataset", default=DATASET, help="chain_evidence 가 있는 질문 json")
    parser.add_argument("--prompts", default="v1,v2", help="쉼표로 구분한 프롬프트 버전")
    parser.add_argument(
        "--judge",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="watch 첫 항목 핵심 로직 판정 (fast 모델, 기본 끔 — 기록 22 변별력 0)",
    )
    parser.add_argument("--cache-dir", default=".repoute_cache", help="clone·인덱스 캐시")
    parser.add_argument("--cache-path", default=DEFAULT_CACHE, help="이 스크립트 전용 설명 캐시")
    parser.add_argument("--no-cache", action="store_true", help="설명 캐시 파일을 지우고 시작")
    parser.add_argument("--out", default=DEFAULT_OUT, help="raw 결과 json")
    parser.add_argument("--dry-run", action="store_true", help="대상과 예상 호출 수만 출력")
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv(".env")  # troubleshooting 06: 경로를 명시해야 키가 읽힌다
    versions = [v.strip() for v in args.prompts.split(",") if v.strip()]
    modules = [importlib.import_module(f"repoute.prompts.explain_{v}") for v in versions]
    items = json.loads(Path(args.dataset).read_text(encoding="utf-8"))

    # 인덱스(LLM 없음)를 열어 레포별 심볼을 고른다. 연결은 끝까지 열어 두고 마지막에 닫는다.
    targets, skipped, indexes = [], [], []
    for repo, locs in evidence_locations(items).items():
        if len(targets) >= MAX_SYMBOLS:
            break
        index = build_index(f"https://github.com/{repo}", args.cache_dir)
        indexes.append(index)
        picked, skips = select_symbols(index, locs, PER_REPO)
        targets += [(index, line, span) for line, span in picked[: MAX_SYMBOLS - len(targets)]]
        skipped += skips

    try:
        calls = len(targets) * len(modules) * (1 + int(args.judge))
        print(
            f"대상 심볼 {len(targets)}개, 프롬프트 {versions}, 판정 {'켬' if args.judge else '끔'}"
        )
        for index, line, span in targets:
            print(
                f"- {index.info.repo_id} {span.file}:{span.line_start}-{span.line_end} "
                f"{span.name} ({span.kind}, 근거 줄 {line}, docstring "
                f"{'있음' if span.docstring else '없음'})"
            )
        for s in skipped:
            print(f"  [건너뜀] {s['repo']} {s['file']}:{s['line']} — {s['reason']}")
        print(f"예상 LLM 호출 수(상한, 캐시 적중 시 줄어듦): {calls}")
        if args.dry_run:
            return

        cache_path = Path(args.cache_path)
        if args.no_cache and cache_path.exists():
            cache_path.unlink()
        llm = get_fast_llm()
        rows = []
        for index, line, span in targets:
            for module in modules:
                try:
                    row = run_one(llm, index, line, span, module, cache_path, args.judge)
                    m, j = row["metrics"], row["judge"]
                    print(
                        f"[ok] {module.VERSION} {span.name}: 유효 {_pct(m['ref_validity'])}, "
                        f"개수 {'O' if m['count_ok'] else 'X'}, "
                        f"첫항목 def/doc {m['first_is_def_or_doc']}, "
                        f"판정 {j['core_logic'] if j else '—'}, {_money(row['cost'])}"
                        + (" (캐시)" if row["cached"] else "")
                    )
                except Exception as e:  # 한 심볼이 실패해도 나머지는 계속 잰다
                    message = str(e).strip().splitlines()[0] if str(e).strip() else ""
                    row = {
                        "repo": index.info.repo_id,
                        "symbol": span.name,
                        "file": span.file,
                        "line_start": span.line_start,
                        "prompt": module.VERSION,
                        "error": f"{type(e).__name__}: {message}",
                    }
                    print(f"[fail] {module.VERSION} {span.name}: {row['error']}")
                rows.append(row)
    finally:
        for index in indexes:
            index.db.close()

    s = load_settings()
    result = {
        "meta": {
            "date": date.today().isoformat(),
            "dataset": args.dataset,
            "prompts": versions,
            "explain_model": s.fast_model,
            "judge_model": s.fast_model if args.judge else None,
            "provider": s.provider,
            "cache_path": str(cache_path),
            "judge_limitation": JUDGE_LIMIT,
            "postprocess": "normalize_watch v1",  # explain_symbol 이 watch 를 후처리한 뒤 잰다
        },
        "skipped": skipped,
        "rows": rows,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(render_table(rows))
    print()
    print(
        "- ref_validity·has_ref_rate 는 심볼별 비율의 평균, 나머지는 해당 심볼 수 / 잴 수 있는 수."
    )
    print("- first_is_def_or_doc 는 낮을수록 좋다 (첫 항목이 def/class 줄·docstring 이면 True).")
    print("- 후처리 적용: normalize_watch v1 (쪼개진 줄 참조 병합, 천 단위 보정, 4개 상한).")
    print(f"- 판정 한계: {JUDGE_LIMIT}")
    failed = [r for r in rows if "error" in r]
    if failed:
        print(f"- 실패 {len(failed)}건 (json 의 error 참고)")
    print(f"저장: {out}")


if __name__ == "__main__":
    main()
