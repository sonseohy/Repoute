"""명령줄 진입점: uv run repoute <명령>."""

import argparse
import os
import sys
import time

from repoute.config import get_fast_llm, get_quality_llm, load_settings
from repoute.graphs.deps import Deps
from repoute.graphs.reading_guide import run_reading_guide
from repoute.indexing.imports import rank_modules
from repoute.indexing.index import build_index


def check() -> None:
    """설정을 보여주고 fast 모델에 핑을 보내 연결을 확인한다."""
    settings = load_settings()
    print(f"provider: {settings.provider}")
    print(f"fast:     {settings.fast_model}")
    print(f"quality:  {settings.quality_model}")
    try:
        llm = get_fast_llm(settings)
        start = time.perf_counter()  # 지연 시간 측정 시작
        response = llm.invoke("Reply with exactly: pong")
        elapsed = time.perf_counter() - start
    except Exception as e:  # 키 누락·네트워크 오류 등은 한 줄로만 알린다
        message = str(e).strip().splitlines()[0] if str(e).strip() else ""
        print(f"오류: {type(e).__name__}: {message}")
        print("API 키/.env 를 확인하세요.")
        sys.exit(1)

    usage = response.usage_metadata or {}  # 제공자가 토큰 수를 안 주면 None
    print(f"응답: {response.content}")
    print(f"시간: {elapsed:.2f}s")
    print(f"토큰: input={usage.get('input_tokens')} output={usage.get('output_tokens')}")
    if os.environ.get("LANGSMITH_TRACING", "").lower() == "true":
        project = os.environ.get("LANGSMITH_PROJECT", "default")
        print(f"LangSmith 트레이스가 '{project}' 프로젝트에 자동 기록됩니다.")


def index(url: str, cache_dir: str) -> None:
    """레포를 인덱싱(또는 캐시 로드)하고 규모·시간·핵심 모듈을 보여준다."""
    try:
        repo = build_index(url, cache_dir)
    except (ValueError, RuntimeError) as e:  # 잘못된 URL·git 실패는 한 줄로만 알린다
        print(f"오류: {e}")
        sys.exit(1)

    symbols, calls = repo.db.count()
    print(f"repo:     {repo.info.repo_id}  ({'cache hit' if repo.cached else 'indexed'})")
    print(f"files:    {repo.file_count}   symbols: {symbols}   calls: {calls}")
    print(f"time:     {repo.elapsed:.2f}s")
    print("core modules (PageRank, 참조 수):")
    for rank, (module, score) in enumerate(rank_modules(repo.graph)[:5], start=1):
        refs = repo.graph.in_degree(module)  # 이 모듈을 import 하는 모듈 수
        print(f"  {rank}. {module:<40} {score:.3f}  ({refs} modules import this)")
    repo.db.close()


def map_repo(url: str, cache_dir: str, validate: bool) -> None:
    """레포 읽기 지도(진입점 + 읽기 순서)를 만들어 출력한다. 실제 LLM 을 호출한다."""
    start = time.perf_counter()
    try:
        repo = build_index(url, cache_dir)
        deps = Deps(get_fast_llm(), get_quality_llm(), repo)
        state = run_reading_guide(deps, validate=validate)
    except Exception as e:  # URL·git·API 키·네트워크 오류는 한 줄로만 알린다
        message = str(e).strip().splitlines()[0] if str(e).strip() else ""
        print(f"오류: {type(e).__name__}: {message}")
        sys.exit(1)
    elapsed = time.perf_counter() - start

    guide, errors = state["guide"], state.get("validation_errors") or []
    # 검증 실패한 "파일:줄" 집합 — 근거가 틀린 설명은 그대로 두지 않고 표시한다 (설계 원칙 2)
    bad = {e.split(" — ")[0] for e in errors}

    def mark(ref) -> str:
        where = f"{ref.file}:{ref.line}"
        return f"{where}  [검증 실패]" if where in bad else where

    print(f"repo: {repo.info.repo_id}")
    print(f"개요: {guide.overview}")
    print("진입점:")
    for ref in guide.entrypoints:
        print(f"  - {mark(ref)}  {ref.symbol or ''}")
    print("읽기 순서:")
    for step in guide.reading_order:
        print(f"  {step.order}. {mark(step.ref)}   {step.reason}")
    print(f"검증: 재시도 {state.get('retry_count', 0)}회, 남은 오류 {len(errors)}개")
    for error in errors:
        print(f"  - {error}")
    print(f"시간: {elapsed:.1f}s   (LLM 비용은 LangSmith 'repoute-dev' 프로젝트에서 확인)")
    repo.db.close()


def main() -> None:
    """서브커맨드를 해석해 해당 함수를 실행한다."""
    parser = argparse.ArgumentParser(prog="repoute")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="LLM 설정과 연결을 확인한다")
    index_parser = sub.add_parser("index", help="레포를 인덱싱하고 핵심 모듈을 보여준다")
    index_parser.add_argument("url", help="GitHub URL 또는 로컬 레포 경로")
    index_parser.add_argument("--cache-dir", default=".repoute_cache", help="clone·인덱스 캐시")
    map_parser = sub.add_parser("map", help="진입점과 추천 읽기 순서를 만든다 (LLM 호출)")
    map_parser.add_argument("url", help="GitHub URL 또는 로컬 레포 경로")
    map_parser.add_argument("--cache-dir", default=".repoute_cache", help="clone·인덱스 캐시")
    map_parser.add_argument("--no-validate", action="store_true", help="인용 검증 루프 끄기")
    args = parser.parse_args()
    if args.command == "check":
        check()
    elif args.command == "index":
        index(args.url, args.cache_dir)
    elif args.command == "map":
        map_repo(args.url, args.cache_dir, validate=not args.no_validate)


if __name__ == "__main__":
    main()
