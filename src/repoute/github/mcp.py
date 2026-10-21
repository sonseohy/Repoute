"""github-mcp-server 원격 엔드포인트 연결과 MCP 도구 헬퍼.

MCP 도구는 비동기 전용(invoke 는 NotImplementedError)이라, 동기 그래프 노드에서는
call_tool_sync 로 감싸 부른다. 도구 설명(스키마)은 매 LLM 호출마다 입력 토큰으로 들어가므로
tool_schema_tokens 로 크기를 재고, 필요한 도구만 names 로 골라 쓴다.
"""

import asyncio
import json
import threading
from collections.abc import Iterable
from typing import Any

from langchain_core.tools import BaseTool

GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
# 이슈 매칭에 필요한 읽기 도구만 (전체 /mcp/ 는 도구 수십 개라 스키마 토큰이 크다)
ISSUE_TOOL_NAMES = ("list_issues", "get_issue", "search_issues")


def github_connection(token: str, toolset: str | None = "issues/readonly") -> dict:
    """MultiServerMCPClient 에 넘길 연결 설정. toolset 이 None 이면 전체 /mcp/ 엔드포인트.

    toolset 을 주면 "x/<toolset>" 경로(예: x/issues/readonly)로 그 도구 묶음만 받는다.
    """
    url = GITHUB_MCP_URL + (f"x/{toolset}" if toolset else "")
    return {
        "transport": "streamable_http",
        "url": url,
        "headers": {"Authorization": f"Bearer {token}"},
    }


def filter_tools(tools: Iterable[BaseTool], names: tuple[str, ...] | None) -> list[BaseTool]:
    """names 에 든 이름의 도구만 남긴다 (순서는 원래 목록 순). names 가 None 이면 전부."""
    if names is None:
        return list(tools)
    wanted = set(names)
    return [t for t in tools if t.name in wanted]


async def get_all_tools(token: str, toolset: str | None = "issues/readonly") -> list[BaseTool]:
    """github-mcp-server 에 연결해 그 엔드포인트의 도구를 전부 받는다 (네트워크 호출, 측정용).

    import 를 함수 안에 둬서, 테스트·dry-run 처럼 연결하지 않는 경로는 MCP 클라이언트를 안 읽는다.
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient({"github": github_connection(token, toolset)})
    return await client.get_tools()


async def load_github_tools(
    token: str,
    toolset: str | None = "issues/readonly",
    names: tuple[str, ...] | None = None,
) -> list[BaseTool]:
    """이슈 매칭에 쓸 MCP 도구. names 를 안 주면 ISSUE_TOOL_NAMES 만 남긴다 (네트워크 호출)."""
    return filter_tools(await get_all_tools(token, toolset), names or ISSUE_TOOL_NAMES)


def _count(text: str) -> int:
    """토큰 수. tiktoken 이 있으면 o200k_base 로 정확히, 없으면 글자 수 / 4 로 추정."""
    try:
        import tiktoken
    except ImportError:
        return len(text) // 4
    return len(tiktoken.get_encoding("o200k_base").encode(text))


def _schema_json(tool: BaseTool) -> str:
    """도구 인자 스키마의 JSON 문자열. MCP 도구는 args_schema 가 dict, 일반 도구는 pydantic 모델."""
    schema = tool.args_schema
    if schema is None:
        return "{}"
    if isinstance(schema, dict):
        return json.dumps(schema, ensure_ascii=False)
    return json.dumps(schema.model_json_schema(), ensure_ascii=False)


def tool_schema_tokens(tools: Iterable[BaseTool]) -> int:
    """도구 설명(name + description + 인자 스키마 JSON)이 LLM 입력에 차지할 토큰 수 추정."""
    text = "".join(f"{t.name}\n{t.description}\n{_schema_json(t)}\n" for t in tools)
    return _count(text)


def _to_text(result: Any) -> str:
    """도구 결과를 문자열로. 문자열이 아니면 JSON 으로 (한글 그대로)."""
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


def call_tool_sync(tool: BaseTool, args: dict) -> str:
    """비동기 전용 도구를 동기 코드에서 부른다.

    실행 중인 이벤트 루프가 없으면(보통: API 는 그래프를 별도 스레드에서 돌린다) asyncio.run.
    이미 루프 안이면 같은 스레드에서 asyncio.run 을 못 쓰므로 새 스레드에서 돌리고 기다린다.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:  # 실행 중인 루프 없음
        return _to_text(asyncio.run(tool.ainvoke(args)))

    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["result"] = asyncio.run(tool.ainvoke(args))
        except BaseException as e:  # 예외는 호출한 스레드에서 다시 던진다
            box["error"] = e

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return _to_text(box["result"])
