# 26. PR Coach 1차 — 변경 심볼·영향 범위·테스트 힌트 정적 재현 (Phase 7, 2026-10-21)

## 방법
- **n = 8 PR** (`evals/datasets/pr_coach_gold.json`: requests 2 · httpx 1 · click 3 · rich 2, 최근 머지 PR 중 소스 `.py` 를 고친 것. raw 는 `evals/datasets/raw/prs_*.json`·`pr_files_*_<n>.json`, 인증 REST 로 수집(`fetch_pr_raw.py`, 사용자 실행). 라벨 = Opus 에이전트 초안·사용자 검수 전. 10개 중 2개 제외: httpx #1(변경 파일이 `httpcore/*`, 인덱스에 없음), requests #6966(`__version__.py` 상수만).
- **라벨**: 변경 심볼(diff 가 건드린 함수·클래스), 영향 심볼(변경 심볼의 **직접 호출자** 중 다시 봐야 할 것 2~6, 호출자가 1개뿐이면 2단계 호출자 포함 — note 에 명시), 테스트 파일 1~3. 인덱스 커밋(shallow, 고정)과 PR 시점이 달라 **심볼 이름 기준**으로 라벨.
- **측정**: `evals/experiments/pr_coach_eval.py` — PR Coach 그래프의 정적 단계(`find_changed_symbols` → `find_impact` → `find_test_files`)만 raw 파일로 재현. **LLM·MCP 0, $0.** 지표: 변경 재현(gold 변경 심볼 중 예측에 있는 비율, 끝 이름), 영향 재현, 영향 정밀(호출자 예측 중 gold 비율), 테스트 hit(파일명).

## before → after (같은 gold, 결정론적 수정 2개)

| 지표 | before (`26-pr-coach-gold.json`) | **after (`…-v2.json`)** |
|---|---|---|
| 변경 심볼 재현 | 28% | **56%** |
| 영향 심볼 재현 | 36% | **64%** |
| 영향 정밀 | 9% | **40%** |
| 테스트 파일 hit | 62% (5/8) | **75% (6/8)** |

레포별 after: requests 변경 100%·영향 83%·정밀 83% / click 38%·75%·36% / rich 67%·58%·22% / httpx(1개) 0%.

**수정 1 — hunk 헤더의 함수 이름을 먼저 쓴다.** before 는 hunk 시작 줄로 `enclosing_symbol` 을 찾았다. PR 커밋과 인덱스 커밋이 다르면 줄이 어긋나 앞 함수가 잡힌다(requests #6963: 헤더 `@@ -219 … @@ def get_netrc_auth(` 인데 인덱스에서는 231줄 → `super_len` 이 잡힘). git 이 헤더에 적어 주는 함수 문맥을 이름으로 파일 안 심볼에서 찾고(여러 개면 가까운 줄), 없을 때만 **첫 변경 줄**(문맥 3줄 뒤)로 fallback. 변경 재현 28 → 56%.
**수정 2 — 피호출자를 기본 0 으로.** 변경 심볼마다 호출자 5 + 피호출자 3 을 넣어 정밀 9% 였다. gold(사람)가 꼽은 영향은 호출자다 — 피호출자는 코드 뷰어에서 변경 심볼을 열면 보이는 것이라 영향 목록에서 뺐다(옵션으로 켤 수 있음). 정밀 9 → 40%, 영향 재현도 올랐다(상한 30 안에서 호출자 자리가 늘어남).

## 실제 흐름 1회 (CLI `repoute pr https://github.com/psf/requests/pull/6767`, 수정 1·2 적용 전)
- 1차 시도는 **존재하지 않는 번호**(#7600, 예시 placeholder)로 404. 서버는 JSON 대신 오류 문장을 돌려주는데 코드가 JSON 으로 읽어 `JSONDecodeError` 로 보고했다 → 오류 문장을 그대로 errors 에 싣도록 수정(테스트 추가). LLM 0회, $0.
- 2차(#6767 "Revert caching a default SSLContext"): MCP `pull_request_read` get·get_files 2회 → 변경 심볼 `_urllib3_request_context`, `HTTPAdapter.proxy_manager_for` → 호출자 3·피호출자 3 → 테스트 힌트 0 → fast 설명 1회. **$0.0003, 14.0s**(LangSmith, 1,060 토큰). 체크포인트 5개 전부 변경·영향 심볼에 인용이 붙었다.
- **품질 관찰**: 요약은 PR 본문을 잘 옮겼지만 체크포인트 문장은 "…영향을 받는지 확인하세요" 류로 일반적이다. 인용은 정확하나 "무엇을 어떻게 확인하라" 가 없다 — fast 모델 1회 + 코드 미열람의 한계. 테스트 힌트 0 은 변경 심볼이 private 메서드라 tests/ 에 이름이 안 나오기 때문(gold 는 `test_requests.py`).

## 관찰
- **정적 영향 범위는 "어디를 다시 봐야 하나" 의 2/3 를 맞춘다(영향 재현 64%, n=8).** 틀리는 쪽은 과잉(정밀 40%): 호출자 5개를 다 넣으니 gold 가 고른 2~3개보다 많다. 사람 라벨은 "변경의 의미상 중요한 호출자" 를 고르는데 그 판단은 정적 분석에 없다 — 여기가 LLM 이 할 일(호출자 목록을 주고 "이 변경에 민감한 것" 을 고르게)이고, 다음 측정 대상.
- **남은 변경 심볼 오탐은 git hunk 문맥의 한계**: 헤더 문맥은 hunk **위쪽** 줄 기준이라 변경이 함수 첫머리에 있으면 앞 함수 이름이 붙는다(click #3884 `get_default` ← 실제는 `get_error_hint`). 모듈 수준(`__all__`) 변경에도 앞 함수가 붙는다(httpx #3699 `main`). 다음 후보: hunk **본문**에서 첫 `+`/`-` 줄 앞의 `def`/`class` 문맥 줄을 헤더보다 먼저 쓰기.
- **인덱스 커밋 고정의 비용**: PR 뒤에 머지된 코드와 어긋남이 이 측정의 노이즈다. PR 의 base/head 커밋으로 인덱스를 만드는 것이 정석이지만 레포당 인덱스가 늘어난다(캐시 키가 repo@commit 이라 구조는 이미 지원).
- httpx 는 PR 1개(`__all__` 만 바뀐 PR)라 0% — 표본이 아니라 그 PR 의 특성. gold 를 8 → 12+ 로 늘릴 때 httpx 코드 PR 을 보충해야 한다.

## 비용 누적
프로젝트 API 비용 약 $4.7 → **약 $4.7**(이번 기록은 $0.0003).
