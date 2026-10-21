"""FastAPI 앱: 인덱싱·코드 보기·Supervisor 실행(SSE)·LangSmith 피드백.

wave01: LLM·체크포인터는 lifespan 에서 한 번만 만든다 (싱글턴).
요청마다 SupervisorDeps 를 새로 만들어 indexes 를 요청별로 둔다 (같은 레포 동시 요청 충돌 방지).
"""

import asyncio
import queue
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.tracers.context import collect_runs
from langsmith.utils import tracing_is_enabled

from repoute.api.schemas import FeedbackRequest, RepoRequest, RunRequest
from repoute.api.sse import format_sse
from repoute.graphs.supervisor import (
    SupervisorDeps,
    ask_user_update,
    build_supervisor_graph,
    turn_input,
)
from repoute.indexing.entrypoints import find_entrypoints
from repoute.indexing.explain import enclosing_symbol, explain_symbol
from repoute.indexing.imports import modules_of, rank_modules_v2
from repoute.indexing.index import build_index
from repoute.indexing.symbols import SymbolDB
from repoute.prompts import explain_v2
from repoute.tools.code_tools import MAX_READ_LINES


def _dump(value):
    """pydantic 모델이면 dict 로, 없으면 None."""
    return value.model_dump() if value is not None else None


def _repo_file(cache_dir, owner: str, name: str, path: str) -> tuple[Path, Path]:
    """(레포 루트, 파일) 경로. 레포 없음·파일 없음은 404, 레포 밖 경로는 400 (read_code 규칙)."""
    cache = Path(cache_dir).resolve()
    root = (cache / owner / name).resolve()
    if not root.is_relative_to(cache) or not root.is_dir():
        raise HTTPException(404, f"레포가 없음: {owner}/{name}")
    file = (root / path).resolve()
    if not file.is_relative_to(root):
        raise HTTPException(400, "레포 밖 경로라 읽을 수 없음")
    if not file.is_file():
        raise HTTPException(404, f"'{path}' 파일이 없음")
    return root, file


def _open_symbol_db(cache_dir, owner: str, name: str) -> tuple[SymbolDB, str] | None:
    """index/<owner>__<name>@*.sqlite 중 가장 최근 파일을 연다. 반환 (DB, repo_id), 없으면 None.

    URL 을 몰라 build_index 를 못 부르므로 이미 만든 인덱스 파일을 읽는다. 쓴 뒤 닫을 것.
    같은 glob 에 걸리는 심볼 임베딩 파일(@<commit>.emb.sqlite)은 뺀다 (troubleshooting 28).
    """
    found = sorted(
        (
            p
            for p in (Path(cache_dir) / "index").glob(f"{owner}__{name}@*.sqlite")
            if not p.name.endswith(".emb.sqlite")
        ),
        key=lambda p: p.stat().st_mtime,
    )
    if not found:
        return None
    return SymbolDB(found[-1]), found[-1].stem.replace("__", "/", 1)


def _find_symbol(cache_dir, owner: str, name: str, path: str, line: int):
    """(레포 루트, 감싸는 심볼, repo_id). 인덱스·심볼이 없으면 404."""
    root, _ = _repo_file(cache_dir, owner, name, path)
    opened = _open_symbol_db(cache_dir, owner, name)
    if opened is None:
        raise HTTPException(404, f"인덱스가 없음: 먼저 인덱싱하세요 ({owner}/{name})")
    db, repo_id = opened
    try:
        span = enclosing_symbol(db, root, path, line)
    finally:
        db.close()
    if span is None:
        raise HTTPException(404, f"'{path}' {line}번째 줄을 감싸는 함수·클래스가 없음")
    return root, span, repo_id


def _run_graph(
    base: SupervisorDeps, repo_url: str, message: str, thread_id: str, out: queue.Queue
) -> None:
    """그래프를 이 스레드에서 끝까지 돌리며 SSE 이벤트를 큐에 넣는다.

    인덱스 SQLite 연결은 만든 스레드에서만 쓸 수 있어 열기·쓰기·닫기를 모두 여기서 한다
    (troubleshooting 04·15). 그래프는 체크포인터로 컴파일해 guide·cursor·history 가 thread 에
    남는다 (wave12). state 는 이번 실행의 업데이트만 모으므로 result 에도 이번 실행 분만 담긴다.

    Phase 6: thread 가 질문(interrupt)에서 멈춰 있으면 turn_input 이 이 메시지를 답으로 넘기는
    Command(resume) 를 만든다. 이번 실행이 질문에서 멈추면 "__interrupt__" 업데이트를
    ProfileRequested 이벤트와 result.ask_user=true·reply=질문 으로 바꾼다.
    Phase 7: pr 의도의 결과는 result.pr_review(PRReview), PR 도구도 이슈 도구처럼 재사용한다.
    """
    deps = SupervisorDeps(
        base.fast_llm,
        base.quality_llm,
        base.cache_dir,
        base.checkpointer,
        embedder=base.embedder,  # 심볼 임베딩 검색 (레포당 첫 질문에서 1회 생성, 이후 파일 캐시)
        embedding_model=base.embedding_model,
        github_token=base.github_token,
        issue_tools=base.issue_tools,  # 앞 요청이 불러 둔 GitHub 도구를 다시 쓴다
        pr_tools=base.pr_tools,  # PR 읽기 도구도 같은 방식 (Phase 7)
    )
    state: dict = {}
    config = {"configurable": {"thread_id": thread_id}}
    try:
        with collect_runs() as cb:  # 루트 실행 id = LangSmith 피드백을 붙일 곳
            graph = build_supervisor_graph(deps, deps.checkpointer)
            start = turn_input(repo_url, message, thread_id, graph)
            for update in graph.stream(start, config=config, stream_mode="updates"):
                for node, values in update.items():
                    if node == "__interrupt__":  # values 는 Interrupt 튜플 (dict 가 아니다)
                        values = ask_user_update(values)
                    values = values or {}
                    events = values.get("events", [])
                    state.update({k: v for k, v in values.items() if k != "events"})
                    out.put(("update", {"node": node, "events": events}))
            if state.get("intent") is None:  # 재개 실행은 classify 를 거치지 않는다
                state["intent"] = graph.get_state(config).values.get("intent")
        if deps.issue_tools is not None:  # 처음 불러온 도구는 다음 요청도 쓰게 base 에 남긴다
            base.issue_tools = deps.issue_tools
        if deps.pr_tools is not None:
            base.pr_tools = deps.pr_tools
        run_id = str(cb.traced_runs[0].id) if cb.traced_runs and tracing_is_enabled() else None
        result = {
            "intent": _dump(state.get("intent")),
            "guide": _dump(state.get("guide")),
            "answer": _dump(state.get("answer")),
            "next": state.get("next"),  # next_step 의 dict 그대로 (pydantic 아님)
            "reply": state.get("reply"),
            "issues": _dump(state.get("issues")),  # IssueMatches (Phase 6)
            "pr_review": _dump(state.get("pr_review")),  # PRReview (Phase 7)
            "ask_user": bool(state.get("ask_user")),  # true 면 reply 가 질문, 다음 메시지가 답
            "validation_errors": state.get("validation_errors") or [],
            "thread_id": thread_id,
            "run_id": run_id,
        }
        out.put(("result", result))
    except Exception as e:  # 잘못된 URL·git 실패·LLM 오류 모두 한 줄로 알린다
        out.put(("error", {"message": str(e).strip().splitlines()[0] if str(e).strip() else ""}))
    finally:
        deps.close()
        out.put(None)  # 종료 신호


def create_app(
    deps: SupervisorDeps | None = None,
    cache_dir: Path | str = ".repoute_cache",
    feedback_client=None,
) -> FastAPI:
    """앱 팩토리. deps 를 주면(테스트) 그 LLM·체크포인터를 쓰고, 없으면 시작할 때 실제로 만든다."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned = deps is None
        if owned:  # import 시점이 아니라 서버 시작 시 1회 (실제 LLM 객체, 호출은 아직 없음)
            from repoute.config import get_embedder, get_fast_llm, get_quality_llm, load_settings
            from repoute.memory.checkpointer import open_checkpointer

            # Supervisor 와 따라가기가 같은 파일을 쓴다. 따라가기는 "<thread>:walkthrough" 칸
            saver = open_checkpointer(Path(cache_dir) / "threads.sqlite")
            settings = load_settings()
            app.state.deps = SupervisorDeps(
                get_fast_llm(),
                get_quality_llm(),
                cache_dir,
                saver,
                embedder=get_embedder(settings),  # 기록 24: 이름 없는 질문 도달 0 → 6/10
                embedding_model=settings.embedding_model,
                # 다른 설정 필드처럼 load_settings 가 채운다. 없으면 이슈 추천은 안내만 한다
                github_token=getattr(settings, "github_token", ""),
            )
        else:
            app.state.deps = deps
        yield
        if owned:
            app.state.deps.checkpointer.conn.close()

    app = FastAPI(title="Repoute API", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],  # Vite dev 서버
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.feedback_client = feedback_client

    @app.get("/health")
    def health():
        """살아 있는지 확인 (LLM 호출 없음)."""
        return {"status": "ok"}

    @app.post("/repos")
    def index_repo(body: RepoRequest):
        """레포를 인덱싱(LLM 없음)하고 규모·핵심 모듈 상위 5개를 준다 (cli.index 와 같은 계산)."""
        try:
            repo = build_index(body.url, app.state.deps.cache_dir)
        except (ValueError, RuntimeError) as e:
            raise HTTPException(400, str(e)) from e
        try:
            symbols, calls = repo.db.count()
            entry = find_entrypoints(repo.db, repo.info.path)
            seeds = modules_of(repo.graph, [s.file for s in entry])  # 진입점 모듈 = 시드
            core = [
                {"module": m, "score": round(score, 4), "refs": repo.graph.in_degree(m)}
                for m, score in rank_modules_v2(repo.graph, seeds)[:5]
            ]
        finally:
            repo.db.close()
        return {
            "repo_id": repo.info.repo_id,
            "files": repo.file_count,
            "cached": repo.cached,
            "symbols": symbols,
            "calls": calls,
            "core_modules": core,
        }

    @app.get("/repos/{owner}/{name}/files")
    def read_file(owner: str, name: str, path: str, start: int = 1, end: int | None = None):
        """코드 뷰어용. read_code 와 같은 규칙: 레포 밖 경로 차단, 최대 200줄."""
        _, file = _repo_file(app.state.deps.cache_dir, owner, name, path)
        start = max(start, 1)
        end = min(end or start + MAX_READ_LINES - 1, start + MAX_READ_LINES - 1)
        if end < start:  # start=10&end=5 같은 역순은 500 이 아니라 400 (code-reading 23 에서 발견)
            raise HTTPException(400, f"end({end}) 가 start({start}) 보다 작음")
        text = file.read_text(encoding="utf-8", errors="replace").splitlines()
        if start > len(text):
            raise HTTPException(400, f"'{path}'에 {start}번째 줄이 없음 (전체 {len(text)}줄)")
        lines = [{"n": n, "text": t} for n, t in enumerate(text[start - 1 : end], start)]
        return {"path": path, "start": start, "end": lines[-1]["n"], "lines": lines}

    @app.get("/repos/{owner}/{name}/symbol")
    def symbol(owner: str, name: str, file: str, line: int) -> dict:
        """line 을 감싸는 가장 안쪽 함수·클래스 범위와 docstring (LLM 없음, $0)."""
        _, span, _ = _find_symbol(app.state.deps.cache_dir, owner, name, file, line)
        return asdict(span)

    @app.get("/repos/{owner}/{name}/explain")
    def explain(owner: str, name: str, file: str, line: int) -> dict:
        """감싸는 심볼이 하는 일·꼭 봐야 할 코드 (fast 모델 1회, 같은 심볼은 캐시에서 $0)."""
        cache_dir = app.state.deps.cache_dir
        root, span, repo_id = _find_symbol(cache_dir, owner, name, file, line)
        cache_path = Path(cache_dir) / "index" / "explanations.sqlite"
        try:
            result, cached = explain_symbol(
                app.state.deps.fast_llm, root, span, cache_path, repo_id, prompts=explain_v2
            )
        except Exception as e:  # LLM 오류는 한 줄로
            message = str(e).strip().splitlines()[0] if str(e).strip() else "LLM 오류"
            raise HTTPException(502, message) from e
        keys = ("name", "kind", "line_start", "line_end")
        return {
            **{k: getattr(span, k) for k in keys},
            **result.model_dump(),
            "cached": cached,
            "prompt_version": explain_v2.VERSION,
        }

    @app.post("/threads/{thread_id}/runs")
    async def run(thread_id: str, body: RunRequest):
        """Supervisor 를 돌리며 진행(update)·결과(result)·오류(error)·끝(done)을 SSE 로 보낸다.

        thread 가 질문(ask_user)에서 멈춰 있으면 이 메시지가 그 답이 된다 (_run_graph 참고).
        """
        out: queue.Queue = queue.Queue()
        args = (app.state.deps, body.repo_url, body.message, thread_id, out)
        threading.Thread(target=_run_graph, args=args, daemon=True).start()

        async def stream():
            loop = asyncio.get_running_loop()
            while (item := await loop.run_in_executor(None, out.get)) is not None:
                yield format_sse(*item)
            yield format_sse("done")

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/feedback")
    def feedback(body: FeedbackRequest):
        """👍/👎·인용 오류 신고를 LangSmith 실행에 붙인다."""
        if not body.run_id:
            raise HTTPException(400, "run_id 가 없음 (트레이싱이 꺼진 실행)")
        if app.state.feedback_client is None:  # 첫 피드백 때 만든다 (테스트는 가짜를 주입)
            from langsmith import Client

            app.state.feedback_client = Client()
        app.state.feedback_client.create_feedback(
            body.run_id, key=body.kind or "user", score=body.score, comment=body.comment
        )
        return {"status": "ok"}

    return app


app = create_app()  # uvicorn 이 import 하는 전역 앱. LLM 은 서버 시작(lifespan) 때 만든다


def main() -> None:
    """uv run repoute-api → http://127.0.0.1:8000 (문서: /docs)."""
    import uvicorn

    uvicorn.run("repoute.api.app:app", host="127.0.0.1", port=8000)
