"""ask 서브커맨드의 인자 정의 테스트 (LLM 호출 없음). 실행: uv run pytest tests/test_cli_ask.py"""

import subprocess


def test_ask_help_shows_thread():
    """repoute ask --help 가 성공하고 --thread·--cache-dir·--prompt·--agent-model 옵션을 보여준다.

    --agent-model 은 실험 09(E5) 에서 추가했다.
    """
    result = subprocess.run(
        ["uv", "run", "repoute", "ask", "--help"], capture_output=True, text=True, check=True
    )
    for option in ("--thread", "--cache-dir", "--prompt", "--agent-model"):
        assert option in result.stdout
