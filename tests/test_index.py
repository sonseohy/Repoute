"""repoute.indexing.index 와 index CLI 테스트. 실행: uv run pytest -v tests/test_index.py"""

import subprocess
import sys

from repoute.indexing.index import build_index

# 원본 레포에 넣을 파일: main 이 util 을 import 하고 helper() 를 부른다
FILES = {
    "app/__init__.py": "",
    "app/main.py": "from app.util import helper\n\n\ndef run():\n    return helper()\n",
    "app/util.py": "def helper():\n    return 1\n",
}


def _make_origin(tmp_path):
    """tmp_path/owner/name 에 FILES 를 커밋한 원본 레포를 만든다 (test_clone 과 같은 방식)."""
    origin = tmp_path / "owner" / "name"
    for rel, text in FILES.items():
        (origin / rel).parent.mkdir(parents=True, exist_ok=True)
        (origin / rel).write_text(text)
    git = ["git", "-C", str(origin)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    commit = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, *commit, "commit", "-q", "-m", "init"], check=True)
    return origin


def test_build_index(tmp_path):
    """처음엔 새로 인덱싱: 파일 3개, 심볼 2개(run·helper), 호출 1개, 엣지 main → util."""
    url = f"file://{_make_origin(tmp_path)}"
    index = build_index(url, cache_dir=tmp_path / "cache")
    assert index.cached is False
    assert index.file_count == 3
    assert index.db.count() == (2, 1)
    assert list(index.graph.edges) == [("app.main", "app.util")]
    assert index.elapsed > 0
    index.db.close()


def test_build_index_cache_hit(tmp_path):
    """두 번째 호출은 인덱스 파일을 읽기만 하고 결과는 처음과 같다 (인덱스 캐시 원칙)."""
    url = f"file://{_make_origin(tmp_path)}"
    first = build_index(url, cache_dir=tmp_path / "cache")
    first.db.close()
    second = build_index(url, cache_dir=tmp_path / "cache")
    assert second.cached is True
    assert second.file_count == first.file_count
    assert second.db.count() == (2, 1)
    assert set(second.graph.edges) == set(first.graph.edges)
    assert second.graph.is_directed()
    names = sorted(p.name for p in (tmp_path / "cache" / "index").iterdir())
    prefix = f"owner__name@{first.info.commit[:7]}"
    assert names == [f"{prefix}.graph.json", f"{prefix}.sqlite"]
    second.db.close()


def _run_cli(*args):
    """repoute CLI 를 별도 프로세스로 실행한다."""
    command = [sys.executable, "-m", "repoute.cli", *args]
    return subprocess.run(command, capture_output=True, text=True)


def test_cli_index(tmp_path):
    """index 명령은 요약과 핵심 모듈을 출력하고 0 으로 끝난다."""
    url = f"file://{_make_origin(tmp_path)}"
    result = _run_cli("index", url, "--cache-dir", str(tmp_path / "cache"))
    assert result.returncode == 0, result.stderr
    assert "repo:" in result.stdout
    assert "core modules" in result.stdout


def test_cli_index_invalid_url(tmp_path):
    """알아볼 수 없는 URL 은 traceback 없이 "오류" 한 줄과 exit 1 로 끝난다."""
    result = _run_cli("index", "not a url", "--cache-dir", str(tmp_path / "cache"))
    assert result.returncode == 1
    assert "오류" in result.stdout
    assert "Traceback" not in result.stderr
