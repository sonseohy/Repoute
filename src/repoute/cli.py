"""명령줄 진입점: uv run repoute <명령>."""

import argparse
import os
import sys
import time

from repoute.config import get_fast_llm, load_settings
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


def main() -> None:
    """서브커맨드를 해석해 해당 함수를 실행한다."""
    parser = argparse.ArgumentParser(prog="repoute")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="LLM 설정과 연결을 확인한다")
    index_parser = sub.add_parser("index", help="레포를 인덱싱하고 핵심 모듈을 보여준다")
    index_parser.add_argument("url", help="GitHub URL 또는 로컬 레포 경로")
    index_parser.add_argument("--cache-dir", default=".repoute_cache", help="clone·인덱스 캐시")
    args = parser.parse_args()
    if args.command == "check":
        check()
    elif args.command == "index":
        index(args.url, args.cache_dir)


if __name__ == "__main__":
    main()
