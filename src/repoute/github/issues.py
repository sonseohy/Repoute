"""MCP 이슈 도구 결과(GitHub API 모양 JSON)를 Issue 목록으로 바꾸고, 입문용 후보를 거른다."""

import json
from typing import Any

from pydantic import BaseModel, field_validator

BODY_LIMIT = 2000  # 본문 저장 상한 (LLM 입력에는 이보다 더 짧게 넣는다)
FRIENDLY_LABELS = ("good first issue", "help wanted", "beginner", "easy")
MAX_COMMENTS = 10  # 라벨이 없을 때 "아직 논의가 길지 않은" 이슈의 댓글 상한


class Issue(BaseModel):
    """이슈 하나. 필요한 필드만 남긴다."""

    number: int
    title: str
    body: str = ""
    labels: list[str] = []
    url: str = ""
    comments: int = 0
    created_at: str = ""

    @field_validator("body", mode="before")
    @classmethod
    def _cut_body(cls, value: Any) -> str:
        """본문이 None 이면 빈 문자열, 길면 BODY_LIMIT 자로 자른다."""
        return (value or "")[:BODY_LIMIT]


def _items(raw: str | list | dict) -> list[dict]:
    """결과에서 이슈 dict 목록을 꺼낸다. 문자열이면 JSON 으로 읽는다.

    list_issues 는 목록 그대로, search_issues 는 {"items": [...]} 또는 {"issues": [...]} 일 수 있다.
    """
    data = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(data, dict):
        data = data.get("items", data.get("issues", []))
    return [item for item in data or [] if isinstance(item, dict)]


def _label_names(labels: list) -> list[str]:
    """labels 는 [{"name": ...}] 또는 이름 문자열 목록일 수 있다."""
    return [lab["name"] if isinstance(lab, dict) else str(lab) for lab in labels or []]


def _comment_count(value: Any) -> int:
    """댓글 수. REST 는 정수, GraphQL 모양은 {"totalCount": n} 일 수 있다."""
    if isinstance(value, dict):
        return int(value.get("totalCount", 0))
    return int(value or 0)


def parse_issues(raw: str | list | dict) -> list[Issue]:
    """MCP list_issues/search_issues 결과 → Issue 목록. pull_request 키가 있는 항목(PR)은 뺀다."""
    issues = []
    for item in _items(raw):
        if "pull_request" in item:  # GitHub API 는 PR 도 이슈 목록에 섞어 준다
            continue
        issues.append(
            Issue(
                number=item["number"],
                title=item.get("title") or "",
                body=item.get("body"),
                labels=_label_names(item.get("labels")),
                url=item.get("html_url") or item.get("url") or "",
                comments=_comment_count(item.get("comments")),
                created_at=item.get("created_at") or "",
            )
        )
    return issues


def good_first_filter(issues: list[Issue], min_body: int = 80) -> list[Issue]:
    """처음 기여하기 좋은 이슈만 남긴다 (순서 유지).

    기준 (둘 중 하나):
    1. 라벨에 "good first issue"·"help wanted"·"beginner"·"easy" 중 하나가 있다 (대소문자 무시).
    2. 그런 라벨이 없으면, 본문이 min_body 자 이상(무엇을 할지 적혀 있음)이고
       댓글이 MAX_COMMENTS 개 이하(논의가 아직 길지 않음)다.
    """
    kept = []
    for issue in issues:
        labels = {label.lower() for label in issue.labels}
        if labels & set(FRIENDLY_LABELS):
            kept.append(issue)
        elif len(issue.body) >= min_body and issue.comments <= MAX_COMMENTS:
            kept.append(issue)
    return kept
