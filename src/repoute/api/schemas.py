"""API 요청 본문 스키마. 응답은 dict 로 돌려준다 (작게 유지)."""

from typing import Literal

from pydantic import BaseModel


class RepoRequest(BaseModel):
    """POST /repos: 인덱싱할 레포 URL."""

    url: str


class RunRequest(BaseModel):
    """POST /threads/{thread_id}/runs: 레포와 사용자 메시지."""

    repo_url: str
    message: str


class FeedbackRequest(BaseModel):
    """POST /feedback: LangSmith 실행(run_id)에 붙일 사용자 평가."""

    run_id: str | None = None  # 트레이싱이 꺼져 있었으면 없다 → 400
    score: Literal[0, 1]  # 👎=0, 👍=1
    comment: str | None = None
    kind: Literal["citation_error", "thumbs"] | None = None  # 없으면 key="user"
