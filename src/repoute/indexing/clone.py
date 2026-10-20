"""레포 URL을 받아 얕게 clone 하고, 캐시에 있으면 재사용한다."""

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# https://github.com/owner/name 또는 git@github.com:owner/name (.git·끝 / 는 미리 떼어 냄)
_GITHUB_URL = re.compile(r"^(?:https://github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+)$")


@dataclass(frozen=True)
class RepoInfo:
    """clone 한 레포 정보. frozen이라 만든 뒤에는 바꿀 수 없다."""

    owner: str
    name: str
    commit: str  # 전체 SHA
    path: Path  # 클론된 디렉터리

    @property
    def repo_id(self) -> str:
        """'owner/name@커밋앞7자리' — 캐시·인덱스 키"""
        return f"{self.owner}/{self.name}@{self.commit[:7]}"


def parse_repo_url(url: str) -> tuple[str, str]:
    """레포 URL에서 (owner, name)을 뽑는다. 못 알아보면 ValueError."""
    text = url.strip().removesuffix("/").removesuffix(".git")
    if match := _GITHUB_URL.match(text):
        return match.group(1), match.group(2)
    # 테스트용 로컬 레포: 마지막 두 경로 요소를 owner/name 으로 쓴다
    if text.startswith(("file://", "/")):
        parts = Path(text.removeprefix("file://")).parts
        if len(parts) >= 3:  # ("/", owner, name) 이상
            return parts[-2], parts[-1]
    raise ValueError(f"레포 URL을 알아볼 수 없음: {url}")


def _git(*args: str) -> str:
    """git 명령을 실행해 표준출력을 돌려준다. 실패하면 RuntimeError."""
    try:
        result = subprocess.run(["git", *args], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        first_line = (error.stderr.strip().splitlines() or [""])[0]
        raise RuntimeError(f"git {args[0]} 실패: {first_line}") from error
    return result.stdout.strip()


def clone_repo(url: str, cache_dir: Path | str = ".repoute_cache") -> RepoInfo:
    """레포를 cache_dir/owner/name 에 clone 한다. 이미 있으면 그대로 쓴다."""
    owner, name = parse_repo_url(url)
    dest = Path(cache_dir) / owner / name
    if dest.exists():
        logger.info("cache hit: %s", dest)
    else:
        logger.info("clone: %s -> %s", url, dest)
        # --depth 1: 최신 커밋 하나만 받아 빠르게 (히스토리는 필요 없음)
        _git("clone", "--depth", "1", url, str(dest))
    commit = _git("-C", str(dest), "rev-parse", "HEAD")
    return RepoInfo(owner, name, commit, dest)
