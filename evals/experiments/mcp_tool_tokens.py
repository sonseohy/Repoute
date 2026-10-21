"""github-mcp-server 엔드포인트별 도구 수와 도구 설명 토큰을 잰다. LLM 호출 0회, MCP 연결만.

도구 설명(name + description + 인자 스키마)은 도구를 붙인 LLM 호출마다 입력 토큰으로 들어간다.
전체 /mcp/ 와 읽기 전용 toolset(x/issues/readonly, x/pull_requests/readonly)을 비교하고,
ISSUE_TOOL_NAMES 로 거른 뒤의 토큰도 함께 본다 (toolset·필터를 쓰는 근거 수치).

실행: uv run python evals/experiments/mcp_tool_tokens.py
      (.env 의 GITHUB_PERSONAL_ACCESS_TOKEN 필요)
확인만: uv run python evals/experiments/mcp_tool_tokens.py --dry-run
"""

import argparse
import asyncio
import sys

from repoute.config import load_settings
from repoute.github.mcp import (
    GITHUB_MCP_URL,
    ISSUE_TOOL_NAMES,
    filter_tools,
    get_all_tools,
    tool_schema_tokens,
)

# (표에 쓸 이름, github_connection 의 toolset 인자)
ENDPOINTS = [
    ("all", None),
    ("issues/readonly", "issues/readonly"),
    ("pull_requests/readonly", "pull_requests/readonly"),
]


def _url(toolset: str | None) -> str:
    return GITHUB_MCP_URL + (f"x/{toolset}" if toolset else "")


async def measure(token: str) -> list[dict]:
    """엔드포인트마다 도구 수·토큰, ISSUE_TOOL_NAMES 필터 뒤 도구 수·토큰."""
    rows = []
    for label, toolset in ENDPOINTS:
        tools = await get_all_tools(token, toolset)
        kept = filter_tools(tools, ISSUE_TOOL_NAMES)
        rows.append(
            {
                "endpoint": label,
                "tools": len(tools),
                "tokens": tool_schema_tokens(tools),
                "filtered_tools": len(kept),
                "filtered_tokens": tool_schema_tokens(kept),
            }
        )
    return rows


def print_table(rows: list[dict]) -> None:
    """마크다운 표로 출력한다 (evals/results 에 그대로 붙일 수 있게)."""
    print("| endpoint | 도구 수 | 설명 토큰 | 필터 뒤 도구 수 | 필터 뒤 토큰 |")
    print("|---|---:|---:|---:|---:|")
    for r in rows:
        print(
            f"| {r['endpoint']} | {r['tools']} | {r['tokens']} "
            f"| {r['filtered_tools']} | {r['filtered_tokens']} |"
        )
    print(f"\n필터: {', '.join(ISSUE_TOOL_NAMES)}")
    print("토큰: tiktoken o200k_base (없으면 글자 수 / 4 추정)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="연결 없이 무엇을 잴지 보여준다")
    args = parser.parse_args()
    if args.dry_run:
        print("dry-run: MCP 연결·LLM 호출 없음. 아래 엔드포인트의 도구 목록을 받아 잰다.")
        for label, toolset in ENDPOINTS:
            print(f"  - {label:<24} {_url(toolset)}")
        print(f"필터 뒤 토큰도 잰다: {', '.join(ISSUE_TOOL_NAMES)}")
        print("비용: LLM 0회 (도구 목록 조회만). 토큰: GITHUB_PERSONAL_ACCESS_TOKEN")
        return
    token = load_settings().github_token
    if not token:
        print("GITHUB_PERSONAL_ACCESS_TOKEN 이 없습니다. .env 에 넣고 다시 실행하세요.")
        sys.exit(1)
    print_table(asyncio.run(measure(token)))


if __name__ == "__main__":
    main()
