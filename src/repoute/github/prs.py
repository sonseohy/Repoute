"""MCP pull_request_read 결과(get·get_files)를 PullRequest 로, diff hunk 를 줄 범위·문맥으로.

github-mcp-server 의 PR 응답 모양은 실서버로 아직 확인하지 않았다. 그래서 REST 모양
(number, title, html_url, user.login, merged_at / filename, status, patch)과
GraphQL 모양(url, author.login, mergedAt / path, changeType)을 둘 다 읽게 방어적으로 파싱한다.
"""

import json
import re
from typing import Any

from pydantic import BaseModel, field_validator

BODY_LIMIT = 2000  # 본문 저장 상한 (LLM 입력에는 이보다 더 짧게 넣는다)
# "@@ -a,b +c,d @@" — 개수(,b ,d)는 1이면 생략될 수 있다
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
# 헤더 줄 전체. 두 번째 @@ 뒤는 git 이 붙이는 문맥(hunk 위쪽의 가장 가까운 def/class 줄)
HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@ ?(.*)$", re.MULTILINE)
# 문맥에서 함수·메서드·클래스 이름: "def f(" / "async def f(" / "class C" (들여쓰기 허용)
CONTEXT_NAME_RE = re.compile(r"^\s*(?:(?:async\s+)?def\s+(\w+)\s*\(|class\s+(\w+))")
PR_URL_RE = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)/?$")


class Hunk(BaseModel):
    """hunk 하나. 줄은 새 파일 기준, context 는 헤더의 두 번째 @@ 뒤 문자열(없으면 None).

    예 "@@ -219,14 +219,7 @@ def get_netrc_auth(url, raise_errors=False):"
    → Hunk(new_start=219, new_end=225, context="def get_netrc_auth(url, raise_errors=False):")
    """

    new_start: int
    new_end: int
    context: str | None = None


class ChangedFile(BaseModel):
    """PR 이 바꾼 파일 하나. changed_ranges 는 새 파일 기준 (시작 줄, 끝 줄) 목록.

    hunks 는 같은 hunk 들에 헤더 문맥까지 붙인 것 (changed_ranges 와 순서·줄이 같다).
    """

    filename: str
    status: str = ""  # added | modified | removed | renamed ...
    additions: int = 0
    deletions: int = 0
    patch: str | None = None
    changed_ranges: list[tuple[int, int]] = []
    hunks: list[Hunk] = []

    @property
    def is_python(self) -> bool:
        """심볼 매핑 대상인가 (.py 파일만)."""
        return self.filename.endswith(".py")


class PullRequest(BaseModel):
    """PR 하나. 필요한 필드만 남긴다."""

    number: int
    title: str
    body: str = ""
    url: str = ""
    author: str = ""
    merged_at: str | None = None
    changed_files: list[ChangedFile] = []

    @field_validator("body", mode="before")
    @classmethod
    def _cut_body(cls, value: Any) -> str:
        """본문이 None 이면 빈 문자열, 길면 BODY_LIMIT 자로 자른다."""
        return (value or "")[:BODY_LIMIT]


def parse_hunks(patch: str | None) -> list[tuple[int, int]]:
    """unified diff 의 hunk 헤더마다 새 파일 기준 줄 범위 (c, c+d-1).

    "@@ -10,3 +12,5 @@" → (12, 16). 삭제만 있는 hunk(d=0)는 지워진 자리 (c, c).
    patch 가 없으면(바이너리·너무 큰 파일) 빈 목록.
    """
    ranges = []
    for match in HUNK_RE.finditer(patch or ""):
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        ranges.append((start, start + count - 1) if count > 0 else (start, start))
    return ranges


def parse_hunk_headers(patch: str | None) -> list[Hunk]:
    """hunk 헤더마다 Hunk(새 시작 줄, 새 끝 줄, 문맥). 줄 범위는 parse_hunks 와 같다.

    문맥은 앞 공백을 지우지 않고(들여쓴 메서드 "    def m(self):" 그대로) 끝 공백만 지운다.
    문맥이 비면 None.
    """
    hunks = []
    for match in HUNK_HEADER_RE.finditer(patch or ""):
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        end = start + count - 1 if count > 0 else start
        context = (match.group(3) or "").rstrip()
        hunks.append(Hunk(new_start=start, new_end=end, context=context or None))
    return hunks


def context_symbol_name(context: str | None) -> str | None:
    """hunk 문맥에서 함수·메서드·클래스 이름. "    def send(self, r):" → "send".

    def / async def / class 줄이 아니면(예 "import os", 변수 대입) None.
    """
    match = CONTEXT_NAME_RE.match(context or "")
    if match is None:
        return None
    return match.group(1) or match.group(2)


def _load(raw: str | list | dict) -> Any:
    """문자열이면 JSON 으로 읽는다 (call_tool_sync 결과는 문자열)."""
    return json.loads(raw) if isinstance(raw, str) else raw


def _unwrap(data: Any, keys: tuple[str, ...]) -> Any:
    """{"pullRequest": {...}} 처럼 한 겹 싸인 모양이면 벗긴다 (keys 순서대로 찾는다)."""
    for key in keys:
        if isinstance(data, dict) and isinstance(data.get(key), dict | list):
            return data[key]
    return data


def _login(value: Any) -> str:
    """작성자. REST 는 user.login, GraphQL 은 author.login, 문자열일 수도 있다."""
    if isinstance(value, dict):
        return str(value.get("login") or "")
    return str(value or "")


def _file_items(raw: str | list | dict) -> list[dict]:
    """get_files 결과의 파일 dict 목록.

    목록 그대로, {"files": [...]}, GraphQL 의 {"files": {"nodes": [...]}} 모양을 읽는다.
    """
    data = _unwrap(_load(raw), ("files", "items"))
    data = _unwrap(data, ("nodes",))
    if not isinstance(data, list):
        raise ValueError(f"get_files 결과를 읽을 수 없음: {type(data).__name__}")
    return [item for item in data if isinstance(item, dict)]


def parse_changed_files(raw: str | list | dict) -> list[ChangedFile]:
    """get_files 결과 → ChangedFile 목록. REST(filename·status) 와 GraphQL(path·changeType) 모양."""
    files = []
    for item in _file_items(raw):
        name = item.get("filename") or item.get("path")
        if not name:
            continue
        patch = item.get("patch")
        files.append(
            ChangedFile(
                filename=name,
                status=str(item.get("status") or item.get("changeType") or "").lower(),
                additions=int(item.get("additions") or 0),
                deletions=int(item.get("deletions") or 0),
                patch=patch,
                changed_ranges=parse_hunks(patch),
                hunks=parse_hunk_headers(patch),
            )
        )
    return files


def parse_pull_request(raw_get: str | dict, raw_files: str | list | dict, repo: str) -> PullRequest:
    """pull_request_read 의 get·get_files 결과 → PullRequest. 못 읽으면 ValueError.

    URL 은 html_url → (github.com 으로 시작하는) url → "https://github.com/{repo}/pull/{n}" 순.
    REST 의 url 은 api.github.com 주소라 쓰지 않는다.
    """
    data = _unwrap(_load(raw_get), ("pullRequest", "pull_request", "data"))
    if not isinstance(data, dict) or "number" not in data:
        raise ValueError("get 결과에 PR 번호(number)가 없음")
    number = int(data["number"])
    url = data.get("html_url") or ""
    if not url and str(data.get("url") or "").startswith("https://github.com/"):
        url = data["url"]
    return PullRequest(
        number=number,
        title=data.get("title") or "",
        body=data.get("body"),
        url=url or f"https://github.com/{repo}/pull/{number}",
        author=_login(data.get("user") or data.get("author")),
        merged_at=data.get("merged_at") or data.get("mergedAt"),
        changed_files=parse_changed_files(raw_files),
    )


def parse_pr_url(url: str) -> tuple[str, str, int] | None:
    """ "https://github.com/o/r/pull/12" → ("o", "r", 12). PR URL 이 아니면 None."""
    match = PR_URL_RE.match(url.strip())
    if match is None:
        return None
    return match.group(1), match.group(2), int(match.group(3))
