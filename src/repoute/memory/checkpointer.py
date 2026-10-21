"""따라가기·Supervisor 대화 스레드를 SQLite 파일에 남기는 체크포인터 (wave12)."""

import sqlite3
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

# 체크포인터가 상태의 pydantic 객체를 되살릴 때 허용할 타입 (langgraph-checkpoint 4.x: 미등록 경고).
# 문자열로만 적어 이 파일이 그래프 모듈을 import 하지 않게 한다 (그래프 → 체크포인터 순환 방지).
# 상태에 새 pydantic 클래스를 넣으면 여기에 더한다. 안에 든 모델(Step·ReadingStep·CodeRef)은
# 바깥 모델이 되살아날 때 함께 검증되므로 따로 적지 않는다.
CHECKPOINT_TYPES = [
    ("repoute.graphs.walkthrough", "WalkthroughAnswer"),  # 따라가기 answer
    ("repoute.graphs.reading_guide", "ReadingGuide"),  # Supervisor guide (wave12)
    ("repoute.graphs.supervisor", "Intent"),  # Supervisor intent
    ("repoute.graphs.issue_matcher", "IssueMatches"),  # Supervisor issues (Phase 6)
    ("repoute.graphs.issue_matcher", "Profile"),  # IssueMatches.profile (상태엔 dict 로 둔다)
]


def open_checkpointer(path: Path | str = ".repoute_cache/threads.sqlite") -> SqliteSaver:
    """대화 스레드 상태를 SQLite 파일에 저장하는 체크포인터.

    wave12: 프로세스가 죽어도 대화가 남는다.

    ":memory:" 를 주면 파일 없이 메모리에만 둔다 (테스트용). 닫을 때는 saver.conn.close().
    """
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)  # 캐시 디렉터리가 없으면 만든다
    # LangGraph 는 체크포인트를 다른 스레드에서 쓸 수 있다 (troubleshooting 04·15 와 같은 뿌리).
    # SqliteSaver 는 내부 락으로 접근을 직렬화하므로 공식 문서도 check_same_thread=False 를 권한다.
    conn = sqlite3.connect(str(path), check_same_thread=False)
    # 상태의 답·지도·의도(pydantic)를 되살릴 수 있게 허용 타입을 등록한다
    serde = JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)
    return SqliteSaver(conn, serde=serde)
