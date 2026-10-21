"""두 정답 데이터셋을 LangSmith Dataset 으로 등록한다 (여러 번 돌려도 같은 결과, idempotent).

- reading-guide-gold: inputs {repo, commit} → outputs {entrypoints, core_modules}
- walkthrough-qa: inputs {repo, question} → outputs {expect_reach, expect_chain}, metadata {id}

실행: uv run python evals/experiments/langsmith_datasets.py --dry-run   (업로드 없이 개수만)
      uv run python evals/experiments/langsmith_datasets.py             (실제 업로드, 네트워크)
"""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).parents[2]
GOLD = ROOT / "evals" / "datasets" / "reading_guide_gold.json"
QA = ROOT / "evals" / "datasets" / "walkthrough_questions.json"


def gold_examples(items: list[dict]) -> list[dict]:
    """읽기 지도 정답 → LangSmith 예제. 레포마다 하나라 metadata.id 는 repo 로 둔다."""
    return [
        {
            "inputs": {"repo": it["repo"], "commit": it["commit"]},
            "outputs": {"entrypoints": it["entrypoints"], "core_modules": it["core_modules"]},
            "metadata": {"id": it["repo"]},
        }
        for it in items
    ]


def qa_examples(items: list[dict]) -> list[dict]:
    """따라가기 질문 → LangSmith 예제. expect_chain 이 아직 없으면 빈 리스트."""
    return [
        {
            "inputs": {"repo": it["repo"], "question": it["question"]},
            "outputs": {
                "expect_reach": it["expect_reach"],
                "expect_chain": it.get("expect_chain", []),
            },
            "metadata": {"id": it["id"]},
        }
        for it in items
    ]


def _key(example) -> str | None:
    """이미 올라간 예제의 식별자: metadata.id, 없으면 inputs.repo."""
    return (example.metadata or {}).get("id") or (example.inputs or {}).get("repo")


def upload(client, name: str, description: str, examples: list[dict]) -> int:
    """데이터셋이 있으면 재사용, 없으면 만든다. 아직 없는 id 의 예제만 추가하고 그 수를 돌려준다.

    LangSmith 0.14 기준: has_dataset·read_dataset·create_dataset·list_examples·create_examples.
    """
    if client.has_dataset(dataset_name=name):
        dataset = client.read_dataset(dataset_name=name)
    else:
        dataset = client.create_dataset(name, description=description)
    seen = {_key(ex) for ex in client.list_examples(dataset_id=dataset.id)}
    new = [ex for ex in examples if ex["metadata"]["id"] not in seen]
    if new:
        client.create_examples(dataset_id=dataset.id, examples=new)
    return len(new)


def _read(path: Path) -> list[dict] | None:
    """json 목록. 파일이 없으면 None (다른 작업이 아직 만들지 않음)."""
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def main() -> None:
    parser = argparse.ArgumentParser(description="정답 데이터셋 LangSmith 등록")
    parser.add_argument("--dry-run", action="store_true", help="업로드 없이 개수만 출력")
    args = parser.parse_args()
    plans = []
    for name, description, path, convert in [
        ("reading-guide-gold", "읽기 지도 정답 (진입점·핵심 모듈)", GOLD, gold_examples),
        ("walkthrough-qa", "따라가기 질문 (도달 심볼·호출 체인)", QA, qa_examples),
    ]:
        items = _read(path)
        if items is None:
            print(f"[skip] {name}: {path.name} 없음")
            continue
        plans.append((name, description, convert(items)))

    if args.dry_run:  # Client 를 만들지 않는다 → 네트워크·API 키 불필요
        for name, _, examples in plans:
            print(f"[dry-run] {name}: 예제 {len(examples)}개")
        return
    from langsmith import Client  # 실제 업로드할 때만 import

    client = Client()
    for name, description, examples in plans:
        added = upload(client, name, description, examples)
        print(f"{name}: 예제 {len(examples)}개 중 새로 추가 {added}개")


if __name__ == "__main__":
    main()
