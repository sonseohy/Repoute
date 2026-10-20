"""명령줄 진입점: uv run repoute <명령>."""

import argparse
import os
import sys
import time

from repoute.config import get_fast_llm, load_settings


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


def main() -> None:
    """서브커맨드를 해석해 해당 함수를 실행한다."""
    parser = argparse.ArgumentParser(prog="repoute")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="LLM 설정과 연결을 확인한다")
    args = parser.parse_args()
    if args.command == "check":
        check()


if __name__ == "__main__":
    main()
