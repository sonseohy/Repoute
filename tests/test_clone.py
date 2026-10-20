"""repoute.indexing.clone(URL 파싱·clone) 테스트. 실행: uv run pytest -v tests/test_clone.py"""

import subprocess

import pytest

from repoute.indexing import clone
from repoute.indexing.clone import RepoInfo, clone_repo, parse_repo_url


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/name",
        "https://github.com/owner/name.git",
        "https://github.com/owner/name/",
        "git@github.com:owner/name.git",
        "file:///tmp/some/owner/name",
        "/tmp/some/owner/name",
    ],
)
def test_parse_repo_url(url):
    """여러 URL 형태(https·ssh·file·로컬 경로)에서 같은 (owner, name)을 뽑는다."""
    assert parse_repo_url(url) == ("owner", "name")


@pytest.mark.parametrize(
    "url", ["", "not a url", "https://github.com/owner", "https://gitlab.com/o/n"]
)
def test_parse_repo_url_invalid(url):
    """알아볼 수 없는 URL은 ValueError 를 낸다 (잘못된 입력을 조용히 넘기지 않는다)."""
    with pytest.raises(ValueError):
        parse_repo_url(url)


def _make_origin(tmp_path):
    """tmp_path/owner/name 에 파일 1개를 커밋한 원본 레포를 만든다."""
    origin = tmp_path / "owner" / "name"
    origin.mkdir(parents=True)
    (origin / "hello.py").write_text("print('hi')\n")
    git = ["git", "-C", str(origin)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    commit = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, *commit, "commit", "-q", "-m", "init"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    return origin, head.stdout.strip()


def test_clone_repo(tmp_path):
    """로컬 원본을 file:// 로 clone 하면 파일과 커밋 SHA가 원본과 같다 (네트워크 없이 검증)."""
    origin, head = _make_origin(tmp_path)
    info = clone_repo(f"file://{origin}", cache_dir=tmp_path / "cache")
    assert info == RepoInfo("owner", "name", head, tmp_path / "cache" / "owner" / "name")
    assert (info.path / "hello.py").exists()
    assert info.repo_id == f"owner/name@{head[:7]}"


def test_clone_repo_cache_hit(tmp_path, monkeypatch):
    """같은 URL 을 두 번 호출하면 git clone 없이 캐시를 재사용한다 (인덱스 캐시 원칙)."""
    origin, _ = _make_origin(tmp_path)
    first = clone_repo(f"file://{origin}", cache_dir=tmp_path / "cache")

    calls = []
    real_run = subprocess.run

    def counting_run(args, **kwargs):
        """subprocess.run 호출 인자를 기록한 뒤 진짜 run 에 넘긴다."""
        calls.append(args)
        return real_run(args, **kwargs)

    monkeypatch.setattr(clone.subprocess, "run", counting_run)
    second = clone_repo(f"file://{origin}", cache_dir=tmp_path / "cache")
    assert second == first
    assert not any("clone" in args for args in calls)


def test_clone_repo_git_error(tmp_path):
    """없는 레포를 clone 하면 git 오류를 RuntimeError 로 감싸 알려 준다."""
    with pytest.raises(RuntimeError, match="git clone 실패"):
        clone_repo(f"file://{tmp_path}/no/repo", cache_dir=tmp_path / "cache")
