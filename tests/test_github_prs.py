"""repoute.github.prs 테스트: hunk 줄 범위와 PR 응답(REST·GraphQL 모양) 파싱. 네트워크 없음.

실행: uv run pytest -v tests/test_github_prs.py
"""

import json

import pytest

from repoute.github.prs import (
    Hunk,
    context_symbol_name,
    parse_changed_files,
    parse_hunk_headers,
    parse_hunks,
    parse_pr_url,
    parse_pull_request,
)

PATCH_TWO_HUNKS = (
    "@@ -10,3 +12,5 @@ def a():\n"
    " ctx\n"
    "+new1\n"
    "+new2\n"
    " ctx\n"
    " ctx\n"
    "@@ -40 +44 @@ class B:\n"
    "-old\n"
    "+new\n"
)


def test_parse_hunks_new_file_range():
    """hunk 헤더 "@@ -10,3 +12,5 @@" 는 새 파일 기준 12~16줄."""
    assert parse_hunks("@@ -10,3 +12,5 @@\n ctx\n") == [(12, 16)]


def test_parse_hunks_delete_only_hunk():
    """삭제만 있는 hunk(+c,0)는 지워진 자리 (c, c) 하나로 본다."""
    assert parse_hunks("@@ -20,2 +19,0 @@\n-gone\n-gone\n") == [(19, 19)]


def test_parse_hunks_multiple_and_count_omitted():
    """hunk 가 여럿이면 순서대로, 개수(,d)가 생략되면 1줄이다. patch 가 없으면 빈 목록."""
    assert parse_hunks(PATCH_TWO_HUNKS) == [(12, 16), (44, 44)]
    assert parse_hunks(None) == [] and parse_hunks("") == []


def test_parse_hunk_headers_context():
    """두 번째 @@ 뒤 문맥을 그대로 읽는다 (def·async def·class·들여쓴 메서드).

    줄 범위는 parse_hunks 와 같다.
    """
    patch = (
        "@@ -219,14 +219,7 @@ def get_netrc_auth(url, raise_errors=False):\n ctx\n"
        "@@ -5 +5,2 @@ async def fetch(client):\n+x\n"
        "@@ -30,3 +31,3 @@ class Session(SessionRedirectMixin):\n ctx\n"
        "@@ -50,2 +51,0 @@     def send(self, request, **kwargs):\n-gone\n"
    )
    assert parse_hunk_headers(patch) == [
        Hunk(new_start=219, new_end=225, context="def get_netrc_auth(url, raise_errors=False):"),
        Hunk(new_start=5, new_end=6, context="async def fetch(client):"),
        Hunk(new_start=31, new_end=33, context="class Session(SessionRedirectMixin):"),
        Hunk(new_start=51, new_end=51, context="    def send(self, request, **kwargs):"),
    ]
    assert [(h.new_start, h.new_end) for h in parse_hunk_headers(patch)] == parse_hunks(patch)


def test_parse_hunk_headers_without_context():
    """문맥이 없거나 공백뿐이면 context 는 None. patch 가 없으면 빈 목록."""
    assert parse_hunk_headers("@@ -1,2 +1,3 @@\n ctx\n@@ -9 +10 @@   \n-a\n+b\n") == [
        Hunk(new_start=1, new_end=3, context=None),
        Hunk(new_start=10, new_end=10, context=None),
    ]
    assert parse_hunk_headers(None) == [] and parse_hunk_headers("") == []


def test_context_symbol_name():
    """def·async def·class 줄에서 이름을 뽑고(들여쓰기 허용), 그 밖의 문맥이나 None 이면 None."""
    assert context_symbol_name("def get_netrc_auth(url, raise_errors=False):") == "get_netrc_auth"
    assert context_symbol_name("async def fetch(client):") == "fetch"
    assert context_symbol_name("    def send(self, request):") == "send"
    assert context_symbol_name("class Session(SessionRedirectMixin):") == "Session"
    assert context_symbol_name("class Empty:") == "Empty"
    assert context_symbol_name("import os") is None
    assert context_symbol_name("default_value = 3") is None
    assert context_symbol_name("    defaults = {}") is None  # def 로 시작하지만 함수 정의가 아니다
    assert context_symbol_name(None) is None and context_symbol_name("") is None


def test_parse_rest_shape():
    """REST 모양: html_url·user.login·merged_at·filename·status·patch 를 읽는다.

    본문은 2000자로 자르고, api.github.com 주소인 url 보다 html_url 을 쓴다.
    """
    raw_get = json.dumps(
        {
            "number": 12,
            "title": "Fix retry",
            "body": "b" * 3000,
            "html_url": "https://github.com/o/r/pull/12",
            "url": "https://api.github.com/repos/o/r/pulls/12",
            "user": {"login": "alice"},
            "merged_at": "2026-10-01T00:00:00Z",
        }
    )
    raw_files = json.dumps(
        [
            {
                "filename": "app/retry.py",
                "status": "modified",
                "additions": 2,
                "deletions": 1,
                "patch": PATCH_TWO_HUNKS,
            }
        ]
    )
    pr = parse_pull_request(raw_get, raw_files, repo="o/r")
    assert (pr.number, pr.title, pr.author, pr.merged_at) == (
        12,
        "Fix retry",
        "alice",
        "2026-10-01T00:00:00Z",
    )
    assert pr.url == "https://github.com/o/r/pull/12" and len(pr.body) == 2000
    (changed,) = pr.changed_files
    assert (changed.filename, changed.status, changed.additions, changed.deletions) == (
        "app/retry.py",
        "modified",
        2,
        1,
    )
    assert changed.changed_ranges == [(12, 16), (44, 44)] and changed.is_python
    assert [(h.new_start, h.context) for h in changed.hunks] == [(12, "def a():"), (44, "class B:")]


def test_parse_graphql_shape_without_url():
    """GraphQL 모양: html_url 이 없고 url 이 API 주소도 아니면 repo·번호로 URL 을 만든다.

    author.login·mergedAt, 파일은 {"files": {"nodes": [{path, changeType}]}} 모양도 읽는다.
    """
    raw_get = {"pullRequest": {"number": 7, "title": "t", "body": None, "author": {"login": "bob"}}}
    raw_files = {"files": {"nodes": [{"path": "src/a.py", "changeType": "MODIFIED"}]}}
    pr = parse_pull_request(raw_get, raw_files, repo="o/r")
    assert pr.url == "https://github.com/o/r/pull/7"
    assert (pr.body, pr.author, pr.merged_at) == ("", "bob", None)
    assert [(f.filename, f.status, f.changed_ranges) for f in pr.changed_files] == [
        ("src/a.py", "modified", [])
    ]


def test_non_python_files_kept_but_not_python():
    """.py 가 아닌 파일도 changed_files 에 남지만 is_python 이 False 라 심볼 매핑에서 빠진다."""
    files = parse_changed_files(
        {"files": [{"filename": "README.md", "patch": "@@ -1 +1 @@\n-a\n+b\n"}]}
    )
    assert [(f.filename, f.is_python, f.changed_ranges) for f in files] == [
        ("README.md", False, [(1, 1)])
    ]


def test_unreadable_get_raises_value_error():
    """number 가 없는 get 결과나 JSON 이 아닌 문자열이면 ValueError.

    그래프(fetch_pr)는 이 예외를 errors 한 줄로 바꾼다.
    """
    with pytest.raises(ValueError):
        parse_pull_request({"message": "Not Found"}, "[]", repo="o/r")
    with pytest.raises(ValueError):
        parse_pull_request("failed to get pull request", "[]", repo="o/r")


def test_parse_pr_url():
    """PR URL 에서 (owner, name, 번호), 레포 URL 이면 None."""
    assert parse_pr_url("https://github.com/psf/requests/pull/6963") == ("psf", "requests", 6963)
    assert parse_pr_url("https://github.com/psf/requests") is None
