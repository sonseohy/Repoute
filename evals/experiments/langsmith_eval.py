"""LangSmith 데이터셋 2개를 evaluate() 로 돌려 Experiments 에 남긴다. 실제 LLM 을 호출한다.

로컬 기록(evals/results)이 주 기록이고, 이 스크립트는 같은 지표를 LangSmith 에도 남겨
프롬프트·agent 모델 버전 간 비교를 UI 에서 보게 한다 (기획서 11.2·11.4).
실행: uv run python evals/experiments/langsmith_eval.py --dataset reading-guide-gold --prompt v3
      uv run python evals/experiments/langsmith_eval.py --dataset walkthrough-qa --dry-run
"""

import argparse
import importlib
import importlib.util
from pathlib import Path

from langchain_core.messages import HumanMessage

from repoute.config import get_fast_llm, get_quality_llm
from repoute.graphs.deps import Deps
from repoute.graphs.walkthrough import Step, WalkthroughAnswer, build_walkthrough_graph
from repoute.indexing.index import build_index
from repoute.validation.citations import CodeRef

ROOT = Path(__file__).parents[2]
CACHE = ".repoute_cache"


def _load(name: str, path: Path):
    """evals/ 는 패키지로 설치되지 않아 파일 경로로 직접 읽는다."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


metrics = _load("metrics", ROOT / "evals" / "evaluators" / "metrics.py")
e4 = _load("e4_validation_loop", ROOT / "evals" / "experiments" / "e4_validation_loop.py")
wt = _load("walkthrough_batch", ROOT / "evals" / "experiments" / "walkthrough_batch.py")
datasets = _load("langsmith_datasets", ROOT / "evals" / "experiments" / "langsmith_datasets.py")


def guide_outputs(row: dict, gold_commit: str) -> dict:
    """e4.run_one 결과 → 실험 outputs. 인덱스 커밋이 gold 커밋과 다르면 commit_mismatch."""
    commit = row["repo"].rsplit("@", 1)[-1]  # repo_id = "owner/name@abc1234"
    out = {k: row[k] for k in ("entrypoints", "reading_modules", "refs")}
    out["validation_errors"] = row["error_messages"]  # 최종 guide 를 같은 기준으로 다시 검증한 것
    if not gold_commit.startswith(commit) and not commit.startswith(gold_commit):
        out["commit_mismatch"] = True
    return out


def walk_outputs(state: dict) -> dict:
    """따라가기 최종 상태 → 실험 outputs (refs 는 인용 순서, answer_text 는 제목+설명)."""
    steps = state["answer"].steps
    refs = [[ref.file, ref.symbol] for step in steps for ref in step.refs]
    errors = state.get("validation_errors") or []
    return {
        "refs": refs,
        "answer_text": "\n".join(f"{s.title}\n{s.explanation}" for s in steps),
        "tool_calls": state.get("tool_calls", 0),
        "read_code_calls": wt.read_code_calls(state["messages"]),
        "strict_pass": not errors and len(refs) > 0,  # walkthrough_batch 의 엄격 통과와 같은 기준
    }


def make_targets(prompt: str, agent_model: str) -> dict:
    """데이터셋 이름 → 타깃 함수 (inputs dict → outputs dict)."""

    def reading_guide(inputs: dict) -> dict:
        row = e4.run_one(inputs["repo"], True, CACHE, prompt)  # gold 채점은 평가자가 한다
        return guide_outputs(row, inputs["commit"])

    def walkthrough(inputs: dict) -> dict:
        prompts = importlib.import_module(f"repoute.prompts.walkthrough_{prompt}")
        index = build_index(f"https://github.com/{inputs['repo']}", CACHE)
        try:
            deps = Deps(get_fast_llm(), get_quality_llm(), index)
            graph = build_walkthrough_graph(deps, prompts, None, agent_model=agent_model)
            start = {"messages": [HumanMessage(inputs["question"])], "tool_calls": 0}
            start |= {"retry_count": 0, "validation_errors": [], "nudges": 0, "explore_blocked": 0}
            return walk_outputs(graph.invoke(start, {"recursion_limit": wt.RECURSION_LIMIT}))
        finally:
            index.db.close()

    return {"reading-guide-gold": reading_guide, "walkthrough-qa": walkthrough}


def _evaluator(score):
    """score(outputs, 정답 outputs) 를 LangSmith 평가자 (run, example) → {key, score} 로 감싼다.

    key 는 함수 이름. 타깃이 실패하면 run.outputs 가 비어 있다 → 점수 None (측정 못 함, 0 아님).
    """

    def evaluator(run, example) -> dict:
        out = run.outputs or None
        return {"key": score.__name__, "score": score(out, example.outputs) if out else None}

    # functools.wraps 는 쓰지 않는다: LangSmith 가 inspect.signature 로 (run, example) 인자를 보는데
    # wraps 가 붙인 __wrapped__ 를 따라가면 (out, gold) 로 보인다. 이름만 옮긴다.
    evaluator.__name__ = score.__name__
    return evaluator


def _refs(out: dict) -> list[tuple]:
    """json 으로 오간 [file, symbol] 목록을 metrics 가 받는 튜플로."""
    return [tuple(r) for r in out["refs"]]


@_evaluator
def entrypoint_recall(out: dict, gold: dict) -> float:
    return metrics.entrypoint_recall(out["entrypoints"], gold["entrypoints"])


@_evaluator
def core_module_precision_at_5(out: dict, gold: dict) -> float:
    return metrics.core_module_precision_at_k(out["reading_modules"], gold["core_modules"], 5)


@_evaluator
def citation_validity(out: dict, gold: dict) -> float:
    """그래프 판정 기준: 최종 validation_errors 가 비면 1.0."""
    return float(not out["validation_errors"])


@_evaluator
def reached(out: dict, gold: dict) -> float:
    """업로드 스키마에 expect_file 이 없어 보통 None (흔한 이름은 한정자로만 판정)."""
    expect, file = gold["expect_reach"], gold.get("expect_file")
    return float(metrics.reached(_refs(out), out["answer_text"], expect, file))


@_evaluator
def call_chain_accuracy(out: dict, gold: dict) -> float | None:
    """기대 체인이 없는 질문은 None (측정 안 함)."""
    chain = gold.get("expect_chain") or []
    pred = metrics.predicted_chain_from_refs(_refs(out))
    return metrics.call_chain_accuracy(pred, chain) if chain else None


@_evaluator
def strict_pass(out: dict, gold: dict) -> float:
    return float(out["strict_pass"])


EVALUATORS = {
    "reading-guide-gold": [entrypoint_recall, core_module_precision_at_5, citation_validity],
    "walkthrough-qa": [reached, call_chain_accuracy, strict_pass],
}


def summarize(name: str, rows: list[list[dict]]) -> str:
    """한 줄 요약: 실험 이름, n, 지표별 평균 (None 은 빼고 평균, 몇 개로 냈는지 함께)."""
    scores: dict[str, list[float]] = {}
    for results in rows:
        for r in results:
            scores.setdefault(r["key"], []).extend([r["score"]] if r["score"] is not None else [])
    parts = [f"{k} {sum(v) / len(v):.2f}(n={len(v)})" if v else f"{k} —" for k, v in scores.items()]
    return f"{name}: n={len(rows)}, " + ", ".join(parts)


def dry_run(dataset: str) -> None:
    """LangSmith·LLM 없이 가짜 outputs 와 로컬 정답 첫 항목으로 평가자 형태만 확인한다."""
    from types import SimpleNamespace

    if dataset == "reading-guide-gold":
        example = datasets.gold_examples(datasets._read(datasets.GOLD))[0]
        gold, commit = example["outputs"], example["inputs"]["commit"]
        row = {"repo": f"x/y@{commit}", "refs": [], "error_messages": []}
        row |= {"entrypoints": gold["entrypoints"], "reading_modules": gold["core_modules"]}
        out = guide_outputs(row, commit)
    else:
        example = datasets.qa_examples(datasets._read(datasets.QA))[0]
        ref = CodeRef(file="a.py", line=1, symbol=example["outputs"]["expect_reach"])
        step = Step(order=1, title="가짜 단계", explanation="형태 확인", refs=[ref])
        answer = WalkthroughAnswer(question="?", steps=[step], summary="")
        out = walk_outputs({"answer": answer, "messages": []})
    run, ex = SimpleNamespace(outputs=out), SimpleNamespace(outputs=example["outputs"])
    print(f"[dry-run] outputs 키: {sorted(out)}")
    print("[dry-run] " + summarize(dataset, [[f(run, ex) for f in EVALUATORS[dataset]]]))


def main() -> None:
    """dry-run 이면 형태만 확인, 아니면 evaluate() 1회 → 요약 한 줄과 결과 URL 출력."""
    parser = argparse.ArgumentParser(description="LangSmith evaluate() 실험 (LLM 호출)")
    parser.add_argument("--dataset", choices=list(EVALUATORS), required=True)
    parser.add_argument("--prompt", choices=["v1", "v2", "v3"], default="v3")
    # --agent-model 은 walkthrough-qa 만 쓴다 (읽기 지도는 fast 요약 + quality 합성 고정)
    parser.add_argument("--agent-model", choices=["fast", "quality"], default="fast")
    parser.add_argument("--experiment-prefix", help="기본 <dataset>-<prompt>[-<agent_model>]")
    parser.add_argument("--max-examples", type=int, help="앞에서 몇 개만 (비용 통제)")
    parser.add_argument("--dry-run", action="store_true", help="LangSmith·LLM 호출 없이 형태만")
    args = parser.parse_args()
    if args.dry_run:
        return dry_run(args.dataset)

    from dotenv import load_dotenv
    from langsmith import Client
    from langsmith.evaluation import evaluate

    load_dotenv(".env")  # troubleshooting 06: 경로를 명시해야 키가 읽힌다
    walk = args.dataset == "walkthrough-qa"
    meta = {"prompt": args.prompt} | ({"agent_model": args.agent_model} if walk else {})
    prefix = args.experiment_prefix or "-".join([args.dataset, *meta.values()])
    data = args.dataset  # 전체면 이름만, 일부면 예제 목록을 직접 넘긴다
    if args.max_examples:
        data = list(Client().list_examples(dataset_name=args.dataset, limit=args.max_examples))
    results = evaluate(
        make_targets(args.prompt, args.agent_model)[args.dataset],
        data=data,
        evaluators=EVALUATORS[args.dataset],
        experiment_prefix=prefix,
        max_concurrency=1,  # 레포 clone·SQLite 를 한 번에 하나씩 (비용·순서 예측 가능)
        metadata=meta,
    )
    rows = [
        [{"key": r.key, "score": r.score} for r in row["evaluation_results"]["results"]]
        for row in results
    ]
    print(summarize(results.experiment_name, rows))
    print(f"결과: {results.url}")


if __name__ == "__main__":
    main()
