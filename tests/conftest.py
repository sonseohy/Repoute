"""pytest 공통 설정. 테스트는 가짜 LLM 으로 돌므로 LangSmith 에 기록하지 않는다 (측정 잡음 방지)."""

import os

# config.py 의 load_dotenv 는 이미 있는 환경변수를 덮어쓰지 않으므로, import 전에 먼저 꺼 둔다
os.environ["LANGSMITH_TRACING"] = "false"
