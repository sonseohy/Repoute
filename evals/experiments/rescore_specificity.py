"""실험 raw(.json)의 인용을 유효성·구체성으로 다시 채점한다. LLM 호출 없음, 캐시된 인덱스만 쓴다.

실행: uv run python evals/experiments/rescore_specificity.py evals/results/04-e6-prompt-v2.json
"""

import argparse
import json
from pathlib import Path

from repoute.indexing.index import build_index
from repoute.validation.citations import CodeRef, citation_specificity, citation_validity


def to_refs(texts: list[str]) -> list[CodeRef]:
    """`파일:줄` 문자열 목록을 CodeRef 로. 경로에 ':' 가 없으니 마지막 ':' 로 나눈다."""
    refs = []
    for text in texts:
        file, line = text.rsplit(":", 1)
        refs.append(CodeRef(file=file, line=int(line)))
    return refs


def rescore(path: Path, cache_dir: str) -> None:
    """json 하나의 row 마다 유효성·구체성을 출력한다. refs 가 없는 row 는 건너뛴다."""
    print(f"== {path}")
    for row in json.loads(path.read_text(encoding="utf-8")):
        label = f"{row.get('repo')} {row.get('mode')} {row.get('prompt', 'v1')}"
        if not row.get("refs"):  # 03 기록은 refs 를 저장하지 않았다
            print(f"  [skip] {label}: refs 없음 (재채점 불가)")
            continue
        owner_name = row["repo"].split("@")[0]
        index = build_index(f"https://github.com/{owner_name}", cache_dir)
        try:
            if index.info.repo_id != row["repo"]:  # 캐시 커밋이 다르면 줄 번호 의미가 달라진다
                print(f"  [warn] 캐시 커밋 {index.info.repo_id} ≠ 기록 {row['repo']}")
            refs = to_refs(row["refs"])
            root = index.info.path
            valid = citation_validity(refs, index.db, root)
            specific = citation_specificity(refs, index.db, root)
            print(f"  {label}: 인용 {len(refs)}, 유효 {valid:.0%}, 구체성 {specific:.0%}")
        finally:
            index.db.close()


def main() -> None:
    """인자로 받은 json 경로들을 차례로 재채점한다."""
    parser = argparse.ArgumentParser(description="raw 결과를 인용 구체성으로 재채점 (LLM 없음)")
    parser.add_argument("paths", nargs="+", type=Path, help="실험 raw .json 경로")
    parser.add_argument("--cache-dir", default=".repoute_cache", help="clone·인덱스 캐시")
    args = parser.parse_args()
    for path in args.paths:
        rescore(path, args.cache_dir)


if __name__ == "__main__":
    main()
