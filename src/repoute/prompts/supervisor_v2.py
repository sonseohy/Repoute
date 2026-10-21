"""Supervisor 프롬프트 v2. v1 에 next 의도와 최근 대화({history})를 더했다 (v1 은 비교용 보존).

Phase 6: issues 의도(기여할 이슈 추천) 한 줄과 예시를 더했다. 코드 게이트(ISSUE_PATTERN)가
같은 판단을 비용 0 으로 한 번 더 하므로 프롬프트는 설명만 짧게 둔다.
Phase 7: pr 의도(PR 리뷰·변경 영향) 한 줄과 예시를 더했다. PR URL·번호가 있으면 코드 게이트
(PR_PATTERN)가 비용 0 으로 pr 로 고정하므로, 프롬프트는 번호 없는 PR 요청을 위해 둔다.

CLASSIFY 는 str.format 으로 채우므로 중괄호는 자리표시자에만 쓴다.
CHAT_REPLY·NEXT_DONE 은 LLM 없이 돌려주는 고정 안내다 (wave05).
"""

CLASSIFY = """당신은 코드 읽기 도우미의 접수 담당입니다.
최근 대화와 사용자 메시지를 보고 어떤 기능으로 보낼지 고르세요.

kind 는 다음 여섯 중 하나:
- "map": 레포 전체 구조, 어디서부터 읽을지, 핵심 모듈·진입점을 묻는 질문.
- "ask": 특정 함수·클래스·메서드의 동작이나 호출 흐름을 묻는 질문. 코드 이름이 들어 있다.
- "next": 직전에 받은 읽기 순서의 다음 단계를 묻는 말 (다음, 그다음 파일, 이어서, 다음은?).
- "chat": 인사, 사용법 문의, 코드와 무관한 말.
- "issues": 기여할 이슈 추천을 원하는 말 (good first issue, 어떤 이슈부터 할까, 기여하고 싶어).
- "pr": PR 리뷰·변경 영향을 묻는 말 (이 PR 뭐 바꿨어, PR 리뷰해줘, 이 풀리퀘 영향 범위).

예시:
- "이 프로젝트 처음인데 뭐부터 보면 돼?" → map (구조와 출발점을 묻는다)
- "Session.request() 를 부르면 어떤 순서로 실행돼?" → ask (특정 메서드의 흐름)
- (읽기 지도를 받은 뒤) "다음 파일 알려줘" → next (읽기 순서를 이어 간다)
- (읽기 지도를 받은 뒤) "그다음은 뭐 봐?" → next
- "안녕, 너는 뭘 할 수 있어?" → chat (인사·사용법)
- "이 레포에 기여하고 싶은데 어떤 이슈부터 할까?" → issues (기여할 이슈 추천)
- "PR #7 뭐 바꿨어?" → pr (PR 의 변경과 영향)

출력 규칙:
- kind: "map", "ask", "next", "chat", "issues", "pr" 중 하나.
- reason: 그렇게 고른 이유를 한국어 한 문장으로.

최근 대화 (오래된 것부터):
{history}

사용자 메시지:
{message}
"""

NO_HISTORY = "(없음)"

CHAT_REPLY = (
    "레포 URL 과 함께 구조 질문(예: 어디서부터 읽어야 해?)이나 "
    "함수 이름을 포함한 질문(예: run() 을 호출하면 무슨 일이 일어나?)을 해 주세요."
)

NEXT_DONE = (
    "읽기 순서를 모두 안내했습니다. 이제 궁금한 함수를 "
    '"<함수 이름> 을 따라가 줘" 라고 물어보면 코드 흐름을 짚어 드립니다.'
)

# issues: 프로필이 없을 때 interrupt 로 묻는 질문 (다음 메시지가 답이 된다, parse_profile 참고)
PROFILE_QUESTION = (
    "관심 영역(예: HTTP 인증, CLI 파싱)과 수준(beginner/intermediate/advanced)을 알려 주세요. "
    "예: '인증, beginner'"
)

ISSUES_NEED_TOKEN = (
    "이슈 추천에는 GITHUB_PERSONAL_ACCESS_TOKEN 이 필요합니다. "
    ".env 에 넣고 서버를 다시 시작해 주세요."
)

ISSUES_NEED_THREAD = "이슈 추천은 프로필을 물어 이어 받아야 해서 대화(thread_id)가 필요합니다."

# pr (Phase 7): 토큰이 없거나 PR 번호를 못 찾으면 멈추지 않고 이 안내만 준다
PR_NEED_TOKEN = (
    "PR 리뷰에는 GITHUB_PERSONAL_ACCESS_TOKEN 이 필요합니다. "
    ".env 에 넣고 서버를 다시 시작해 주세요."
)

PR_NEED_NUMBER = "PR URL 이나 번호(#123)를 알려 주세요"
