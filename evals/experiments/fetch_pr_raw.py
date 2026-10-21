"""PR Coach gold 용 raw 수집: 레포 4개의 최근 머지 PR 중 .py 를 고친 것 2~3개씩 + 파일 목록.

GitHub REST 를 .env 의 GITHUB_PERSONAL_ACCESS_TOKEN 으로 인증 호출한다 (한도 5,000/시간).
LLM 없음. 사용자가 직접 실행한다:

    uv run python evals/experiments/fetch_pr_raw.py            # 레포당 목록 1회 + 파일 목록 ≤3회
    uv run python evals/experiments/fetch_pr_raw.py --dry-run  # 호출 계획만

저장(API 응답 그대로): evals/datasets/raw/prs_<owner>__<name>.json,
      pr_files_<owner>__<name>_<n>.json
"""

import argparse
import json
import os
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

REPOS = ["psf/requests", "encode/httpx", "pallets/click", "Textualize/rich"]
RAW_DIR = Path("evals/datasets/raw")
PER_REPO = 3  # 고를 PR 수 (코드 PR 이 모자라면 적게)
LIST_SIZE = 30  # 목록 한 페이지


def _get(url: str, token: str):
    """인증 GET → JSON. 오류 본문은 그대로 예외 메시지로."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def is_code_pr(files: list[dict]) -> bool:
    """소스 .py 를 하나 이상 고친 PR (docs/·tests/ 만 고친 것은 제외)."""
    for f in files:
        name = f.get("filename", "")
        if name.endswith(".py") and not name.startswith(("docs/", "tests/", "test/")):
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--per-repo", type=int, default=PER_REPO)
    args = parser.parse_args()
    load_dotenv(".env")
    token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
    if args.dry_run:
        most = len(REPOS) * (args.per_repo + 3)
        print(f"레포 {len(REPOS)}개: 목록 {len(REPOS)}회 + 파일 목록 최대 {most}회")
        print("토큰:", "있음" if token else "없음 (.env 의 GITHUB_PERSONAL_ACCESS_TOKEN 필요)")
        return
    if not token:
        raise SystemExit(".env 의 GITHUB_PERSONAL_ACCESS_TOKEN 이 필요합니다")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    calls = 0
    for repo in REPOS:
        owner, name = repo.split("/")
        url = (
            f"https://api.github.com/repos/{repo}/pulls"
            f"?state=closed&per_page={LIST_SIZE}&sort=updated&direction=desc"
        )
        pulls = _get(url, token)
        calls += 1
        (RAW_DIR / f"prs_{owner}__{name}.json").write_text(json.dumps(pulls, ensure_ascii=False))
        picked = 0
        for pr in pulls:
            if not pr.get("merged_at") or picked >= args.per_repo:
                continue
            files = _get(
                f"https://api.github.com/repos/{repo}/pulls/{pr['number']}/files?per_page=100",
                token,
            )
            calls += 1
            if not is_code_pr(files):
                continue
            path = RAW_DIR / f"pr_files_{owner}__{name}_{pr['number']}.json"
            path.write_text(json.dumps(files, ensure_ascii=False))
            picked += 1
            print(f"{repo} #{pr['number']} {pr['title'][:50]} — 파일 {len(files)}개 → {path.name}")
        if picked < 2:
            print(f"  [경고] {repo}: 코드 PR {picked}개만 찾음 (목록 {LIST_SIZE}개 안에서)")
    print(f"REST 호출 {calls}회 (인증, 토큰은 저장하지 않음)")


if __name__ == "__main__":
    main()
