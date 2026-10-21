"""따라가기 대화 스레드를 SQLite 파일에 남기는 체크포인터 (wave12)."""

import sqlite3
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from repoute.graphs.walkthrough import CHECKPOINT_TYPES


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
    # 상태의 답(WalkthroughAnswer)을 되살릴 수 있게 허용 타입을 등록한다
    serde = JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)
    return SqliteSaver(conn, serde=serde)
