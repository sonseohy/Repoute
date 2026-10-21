"""repoute.github.mcp 테스트. MCP 서버에 연결하지 않고 가짜 비동기 도구만 쓴다.

실행: uv run pytest -v tests/test_github_mcp.py
"""

import asyncio

import pytest
from langchain_core.tools import BaseTool

from repoute.github.mcp import (
    GITHUB_MCP_URL,
    ISSUE_TOOL_NAMES,
    call_tool_sync,
    filter_tools,
    github_connection,
    tool_schema_tokens,
)


class AsyncOnlyTool(BaseTool):
    """MCP 도구처럼 비동기 실행만 되는 가짜 도구. 동기 _run 은 막혀 있다."""

    name: str = "list_issues"
    description: str = "List issues in a GitHub repository."
    result: object = None  # ainvoke 가 돌려줄 값 (None 이면 인자를 그대로 돌려준다)

    def _run(self, **kwargs):
        raise NotImplementedError("StructuredTool does not support sync invocation.")

    async def _arun(self, **kwargs):
        await asyncio.sleep(0)  # 실제로 이벤트 루프를 거치게 한다
        if isinstance(self.result, Exception):
            raise self.result
        return kwargs if self.result is None else self.result


def _tool(name: str, result: object = None) -> AsyncOnlyTool:
    """이름과 반환값을 정한 가짜 도구."""
    return AsyncOnlyTool(name=name, result=result)


def test_connection_uses_readonly_toolset_url_and_bearer():
    """기본 연결은 x/issues/readonly·streamable_http·Bearer 헤더, toolset=None 이면 전체 /mcp/."""
    conn = github_connection("tok")
    assert conn == {
        "transport": "streamable_http",
        "url": GITHUB_MCP_URL + "x/issues/readonly",
        "headers": {"Authorization": "Bearer tok"},
    }
    assert github_connection("tok", "pull_requests/readonly")["url"].endswith(
        "/mcp/x/pull_requests/readonly"
    )
    assert github_connection("tok", None)["url"] == "https://api.githubcopilot.com/mcp/"


def test_filter_tools_keeps_only_named_tools_in_order():
    """filter_tools 는 이름이 names 에 있는 도구만 원래 순서대로 남기고, None 이면 전부 둔다."""
    tools = [_tool("get_issue"), _tool("create_issue"), _tool("list_issues")]
    kept = filter_tools(tools, ISSUE_TOOL_NAMES)
    assert [t.name for t in kept] == ["get_issue", "list_issues"]
    assert len(filter_tools(tools, None)) == 3


def test_async_only_tool_rejects_sync_invoke():
    """가짜 도구가 MCP 도구처럼 동기 invoke 를 거부하는지 (call_tool_sync 가 필요한 이유)."""
    with pytest.raises(NotImplementedError):
        _tool("list_issues").invoke({})


def test_call_tool_sync_without_running_loop_returns_json_text():
    """루프가 없으면 call_tool_sync 는 ainvoke 결과를 받고, 문자열이 아니면 JSON 으로 바꾼다."""
    tool = _tool("list_issues", result=[{"number": 1, "title": "한글 제목"}])
    assert call_tool_sync(tool, {}) == '[{"number": 1, "title": "한글 제목"}]'
    assert call_tool_sync(_tool("get_issue", result="그대로"), {}) == "그대로"


def test_call_tool_sync_inside_running_loop_uses_thread():
    """이미 이벤트 루프 안(예: async 함수)에서 불러도 새 스레드에서 실행해 결과를 돌려준다."""

    async def caller() -> str:
        return call_tool_sync(_tool("get_issue", result="ok"), {})

    assert asyncio.run(caller()) == "ok"


def test_call_tool_sync_reraises_tool_error_in_both_paths():
    """도구 예외는 루프가 없을 때도, 루프 안(스레드 경로)에서도 호출한 쪽에 그대로 전달된다."""
    tool = _tool("list_issues", result=RuntimeError("401 Unauthorized"))
    with pytest.raises(RuntimeError, match="401"):
        call_tool_sync(tool, {})

    async def caller() -> str:
        return call_tool_sync(tool, {})

    with pytest.raises(RuntimeError, match="401"):
        asyncio.run(caller())


def test_tool_schema_tokens_positive_and_grows_with_tools():
    """도구 설명 토큰 추정은 양수이고, 도구가 늘면 커진다 (빈 목록은 0)."""
    one = tool_schema_tokens([_tool("list_issues")])
    two = tool_schema_tokens([_tool("list_issues"), _tool("get_issue")])
    assert one > 0 and two > one
    assert tool_schema_tokens([]) == 0
