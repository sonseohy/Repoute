"""읽기 지도 프롬프트 v1. str.format 으로 채우므로 중괄호는 자리표시자에만 쓴다."""

SUMMARIZE_MODULE = """당신은 꼼꼼한 코드 리뷰어입니다. 아래 파이썬 모듈 하나의 역할을 요약하세요.

모듈 이름: {module}
파일 경로: {file}

정의된 심볼 (이름, 종류, 시작 줄):
{symbols}

파일 앞부분 코드 (줄 번호 포함):
{code}

출력 규칙:
- module, file 은 위 값을 그대로 쓴다.
- summary: 이 모듈의 역할을 한국어 2~3문장으로.
- role: 이 모듈이 레포에서 맡는 종류. "entry", "core", "util", "config", "other" 중 하나.
- 코드에 없는 내용을 지어내지 말 것. 모르면 모른다고 쓴다.
"""

SYNTHESIZE_GUIDE = """당신은 오픈소스 온보딩 멘토입니다. 처음 온 기여자가 레포 {repo_id} 를
어떤 순서로 읽으면 좋을지 안내하세요.

모듈 요약:
{summaries}

import 관계 (A → B 는 "A 가 B 를 import 한다", 상위 30개):
{edges}

진입점 후보 (파일:줄 함수명):
{entrypoints}

PageRank 상위 모듈 (많이 참조될수록 위):
{ranked}

이전 답의 인용 검증 오류 (있으면 반드시 고칠 것):
{feedback}

출력 규칙:
- overview: 프로젝트 한 줄 개요 (한국어).
- entrypoints: 진입점 후보 중 실제 진입점을 CodeRef(file, line, symbol) 로.
- reading_order: 5~8단계. 의존성이 없는 것(다른 모듈이 import 하는 쪽)부터 먼저 읽는 순서.
- 각 단계: order(1부터), file, reason(한국어 1~2문장), ref(CodeRef).
- 각 단계와 진입점에는 반드시 실제 존재하는 파일:줄 인용(CodeRef)을 붙일 것.
- 인용의 줄 번호는 입력으로 받은 심볼 목록·진입점 후보의 줄 번호만 사용할 것.
- 위 입력에 없는 파일이나 심볼을 지어내지 말 것.
"""
