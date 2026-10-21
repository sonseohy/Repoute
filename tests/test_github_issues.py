"""repoute.github.issues(이슈 파싱·입문용 필터) 테스트. 네트워크 없음.

실행: uv run pytest -v tests/test_github_issues.py
"""

import json

from repoute.github.issues import BODY_LIMIT, Issue, good_first_filter, parse_issues

LONG_BODY = "재현 방법과 기대 동작이 적힌 충분히 긴 본문입니다. " * 5  # 80자 이상

# GitHub REST API 모양 (github-mcp-server 가 돌려주는 JSON 과 같은 키)
RAW = [
    {
        "number": 12,
        "title": "Fix typo in docs",
        "body": "x" * 3000,
        "labels": [{"name": "good first issue"}, {"name": "docs"}],
        "html_url": "https://github.com/o/r/issues/12",
        "comments": 2,
        "created_at": "2026-10-01T00:00:00Z",
    },
    {
        "number": 13,
        "title": "Add retry option",
        "body": None,
        "labels": [],
        "html_url": "https://github.com/o/r/pull/13",
        "comments": 0,
        "created_at": "2026-10-02T00:00:00Z",
        "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/13"},
    },
    {
        "number": 14,
        "title": "Timeout is ignored",
        "body": None,
        "labels": [],
        "html_url": "https://github.com/o/r/issues/14",
        "comments": 0,
        "created_at": "2026-10-03T00:00:00Z",
    },
]


def test_parse_issues_skips_prs_and_reads_fields():
    """JSON 문자열을 읽어 PR(pull_request 키)을 빼고, 라벨 이름·URL·댓글 수를 꺼낸다."""
    issues = parse_issues(json.dumps(RAW))
    assert [i.number for i in issues] == [12, 14]
    first = issues[0]
    assert first.labels == ["good first issue", "docs"]
    assert first.url == "https://github.com/o/r/issues/12"
    assert (first.comments, first.created_at) == (2, "2026-10-01T00:00:00Z")
    assert issues[1].body == ""  # 본문 None 은 빈 문자열


def test_parse_issues_cuts_body_to_limit():
    """본문은 BODY_LIMIT(2000)자까지만 저장한다."""
    issues = parse_issues(RAW)  # 이미 파싱된 리스트도 받는다
    assert BODY_LIMIT == 2000
    assert len(issues[0].body) == 2000


def test_parse_issues_accepts_search_result_shapes():
    """search_issues 모양 {"items": [...]} 와 {"issues": [...]} 를 모두 읽는다."""
    assert [i.number for i in parse_issues({"items": RAW})] == [12, 14]
    assert [i.number for i in parse_issues(json.dumps({"issues": RAW[:1]}))] == [12]
    assert parse_issues({"total_count": 0}) == []


def test_parse_issues_accepts_graphql_comment_count_and_string_labels():
    """댓글 수가 {"totalCount": n}, 라벨이 문자열 목록이어도 읽는다 (MCP 서버 버전 차이 대비)."""
    raw = [{"number": 1, "title": "t", "labels": ["bug"], "comments": {"totalCount": 4}}]
    issue = parse_issues(raw)[0]
    assert (issue.labels, issue.comments) == (["bug"], 4)


def test_good_first_filter_by_label_case_insensitive():
    """입문 라벨(대소문자 무시)이 있으면 본문이 짧아도 남는다."""
    issues = [
        Issue(number=1, title="a", labels=["Good First Issue"]),
        Issue(number=2, title="b", labels=["HELP WANTED"], comments=50),
        Issue(number=3, title="c", labels=["bug"]),
    ]
    assert [i.number for i in good_first_filter(issues)] == [1, 2]


def test_good_first_filter_without_label_needs_body_and_few_comments():
    """입문 라벨이 없으면 본문 80자 이상 + 댓글 10개 이하만 남는다."""
    issues = [
        Issue(number=1, title="long", body=LONG_BODY, comments=10),
        Issue(number=2, title="short", body="짧음", comments=0),
        Issue(number=3, title="busy", body=LONG_BODY, comments=11),
    ]
    assert [i.number for i in good_first_filter(issues)] == [1]
    assert [i.number for i in good_first_filter(issues, min_body=1)] == [1, 2]


def test_parse_real_mcp_blocks_fixture():
    """실서버 응답(콘텐츠 블록 목록, labels·html_url 없음)을 _to_text → parse_issues 로 읽는다.

    tests/fixtures/mcp_list_issues_sample.json 은 2026-10-21 psf/requests list_issues 응답 앞 3개
    (본문 200자로 절단). 라벨은 빈 목록, URL 은 repo 와 번호로 만들어진다.
    """
    import json
    from pathlib import Path

    from repoute.github.mcp import _to_text

    blocks = json.loads(Path("tests/fixtures/mcp_list_issues_sample.json").read_text())
    issues = parse_issues(_to_text(blocks), repo="psf/requests")
    assert [i.number for i in issues][:1] == [7634] and len(issues) == 3
    assert issues[0].labels == [] and issues[0].comments == 9
    assert issues[0].url == "https://github.com/psf/requests/issues/7634"
    assert "AttributeError" in issues[0].title
