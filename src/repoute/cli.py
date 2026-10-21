"""명령줄 진입점: uv run repoute <명령>."""

import argparse
import importlib
import os
import sys
import time
from pathlib import Path

from repoute.config import get_fast_llm, get_quality_llm, load_settings
from repoute.graphs.deps import Deps
from repoute.graphs.reading_guide import run_reading_guide
from repoute.graphs.walkthrough import ask, build_walkthrough_graph
from repoute.indexing.entrypoints import find_entrypoints
from repoute.indexing.imports import modules_of, rank_modules_v2
from repoute.indexing.index import build_index
from repoute.memory.checkpointer import open_checkpointer
from repoute.validation.citations import citation_at_def


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
    entry = find_entrypoints(repo.db, repo.info.path)
    seeds = modules_of(repo.graph, [s.file for s in entry])  # 진입점이 있는 모듈 = 시드
    print("core modules (랭킹 v2: 역방향 개인화 PageRank, 참조 수):")
    for rank, (module, score) in enumerate(rank_modules_v2(repo.graph, seeds)[:5], start=1):
        refs = repo.graph.in_degree(module)  # 이 모듈을 import 하는 모듈 수
        print(f"  {rank}. {module:<40} {score:.3f}  ({refs} modules import this)")
    repo.db.close()


def map_repo(url: str, cache_dir: str, validate: bool, prompt: str = "v3") -> None:
    """레포 읽기 지도(진입점 + 읽기 순서)를 만들어 출력한다. 실제 LLM 을 호출한다."""
    prompts = importlib.import_module(f"repoute.prompts.reading_guide_{prompt}")
    start = time.perf_counter()
    try:
        repo = build_index(url, cache_dir)
        deps = Deps(get_fast_llm(), get_quality_llm(), repo)
        state = run_reading_guide(deps, validate=validate, prompts=prompts)
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


def ask_repo(
    url: str,
    question: str,
    thread: str | None,
    cache_dir: str,
    prompt: str = "v3",
    agent_model: str = "quality",
) -> None:
    """질문 하나로 호출 체인을 따라가 근거와 함께 출력한다. 실제 LLM 을 호출한다.

    대화는 cache_dir/threads.sqlite 에 남아 --thread 로 이어 갈 수 있다 (wave12).
    agent_model 은 도구를 고르는 agent 노드의 모델만 바꾼다 (finalize 는 항상 quality, 실험 E5).
    """
    prompts = importlib.import_module(f"repoute.prompts.walkthrough_{prompt}")
    start = time.perf_counter()
    try:
        repo = build_index(url, cache_dir)
        thread_id = thread or f"{repo.info.repo_id}:default"  # 기본 스레드는 레포별로 분리
        deps = Deps(get_fast_llm(), get_quality_llm(), repo)
        saver = open_checkpointer(Path(cache_dir) / "threads.sqlite")
        graph = build_walkthrough_graph(deps, prompts, saver, agent_model=agent_model)
        state = ask(graph, question, thread_id)
    except Exception as e:  # URL·git·API 키·네트워크 오류는 한 줄로만 알린다
        message = str(e).strip().splitlines()[0] if str(e).strip() else ""
        print(f"오류: {type(e).__name__}: {message}")
        sys.exit(1)
    elapsed = time.perf_counter() - start

    answer, errors = state["answer"], state.get("validation_errors") or []
    bad = {e.split(" — ")[0] for e in errors}  # 검증 실패한 "파일:줄" (map_repo 와 같은 방식)

    def mark(ref) -> str:
        where = f"{ref.file}:{ref.line}" + (f" [{ref.symbol}]" if ref.symbol else "")
        return f"{where} [검증 실패]" if f"{ref.file}:{ref.line}" in bad else where

    print(f"repo: {repo.info.repo_id}   thread: {thread_id}")
    print(f"질문: {question}")
    for step in answer.steps:
        print(f"{step.order}. {step.title}")
        print(f"   {step.explanation}")
        print(f"   근거: {', '.join(mark(ref) for ref in step.refs) or '없음'}")
    print(f"요약: {answer.summary}")
    refs = [ref for step in answer.steps for ref in step.refs]
    valid = sum(f"{ref.file}:{ref.line}" not in bad for ref in refs)  # validate 결과 기준
    at_def = round(citation_at_def(refs, repo.db, repo.info.path) * len(refs))
    print(
        f"도구 호출: {state.get('tool_calls', 0)}회   "
        f"재작성: {state.get('retry_count', 0)}회   남은 오류: {len(errors)}개   "
        f"인용: 유효 {valid}/{len(refs)}, def 줄 {at_def}/{len(refs)}"
    )
    for error in errors:
        print(f"  - {error}")
    settings = load_settings()  # 모델 이름만 읽는다 (네트워크 없음)
    used = settings.fast_model if agent_model == "fast" else settings.quality_model
    print(f"agent 모델: {agent_model} ({used})   finalize: quality ({settings.quality_model})")
    print(f"시간: {elapsed:.1f}s   (LLM 비용은 LangSmith 'repoute-dev' 에서 확인)")
    print(f'같은 대화를 이어가려면: repoute ask {url} "<다음 질문>" --thread {thread_id}')
    saver.conn.close()
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
    map_parser.add_argument(
        "--prompt", choices=["v1", "v2", "v3"], default="v3", help="프롬프트 버전"
    )
    ask_parser = sub.add_parser("ask", help="질문 하나로 호출 체인을 따라간다 (LLM 호출)")
    ask_parser.add_argument("url", help="GitHub URL 또는 로컬 레포 경로")
    ask_parser.add_argument("question", help='예: "create_app() 을 부르면 무슨 일이 일어나?"')
    ask_parser.add_argument("--thread", help="이어 갈 대화 ID (기본: <repo_id>:default)")
    ask_parser.add_argument("--cache-dir", default=".repoute_cache", help="clone·인덱스·대화 캐시")
    ask_parser.add_argument(
        "--prompt", choices=["v1", "v2", "v3"], default="v3", help="프롬프트 버전"
    )
    ask_parser.add_argument(
        "--agent-model",
        choices=["fast", "quality"],
        default="quality",
        help="도구를 고르는 agent 노드의 모델 (finalize 는 항상 quality)",
    )
    args = parser.parse_args()
    if args.command == "check":
        check()
    elif args.command == "index":
        index(args.url, args.cache_dir)
    elif args.command == "map":
        map_repo(args.url, args.cache_dir, validate=not args.no_validate, prompt=args.prompt)
    elif args.command == "ask":
        ask_repo(
            args.url,
            args.question,
            args.thread,
            args.cache_dir,
            prompt=args.prompt,
            agent_model=args.agent_model,
        )


if __name__ == "__main__":
    main()
