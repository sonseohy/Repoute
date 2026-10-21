"""Server-Sent Events 한 덩어리를 만드는 함수 (wave03a)."""

import json


def format_sse(event: str, data: dict | None = None) -> str:
    """'event: 이름\\ndata: JSON\\n\\n' 형식. 한글은 그대로 둔다(ensure_ascii=False)."""
    return f"event: {event}\ndata: {json.dumps(data or {}, ensure_ascii=False)}\n\n"
