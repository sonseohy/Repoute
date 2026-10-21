# 21. walkthrough-qa 첫 LangSmith Experiment (Phase 4, 2026-10-21)

업로드한 walkthrough-qa 데이터셋을 `evaluate()` 로 처음 실행. 기록 13(n=20, 로컬 배치)과 같은 설정(quality agent, v3, 탐색 상한).

- **n = 5** (LangSmith 가 돌려준 순서의 앞 5개: click-05, click-01, rich-02, click-04, rich-05). Experiment `walkthrough-qa-v3-quality-0ae83013`. 비용 $0.394 (질문당 $0.079). LangSmith 에 같은 이름 패턴의 Experiment 가 하나 더 있음(`…-7244be10`) — 중복 실행으로 보이며, 비용은 두 배일 수 있다.

## 결과

| 질문 | reached | call_chain_accuracy | strict_pass | 도구 / 읽기 | 비용 |
|---|---|---|---|---|---|
| click-05 required 옵션 누락 → MissingParameter | 1.0 | 0.50 | 1.0 | 15 / 7 | $0.088 |
| click-01 cli() 호출 → Command.invoke | 0.0 | 0.00 | 1.0 | 7 / 2 | $0.061 |
| rich-02 [bold] 마크업 → render_str | 1.0 | 1.00 | 1.0 | 15 / 4 | $0.106 |
| click-04 없는 하위 명령 → resolve_command | 0.0 | 0.00 | 1.0 | 7 / 3 | $0.051 |
| rich-05 print_exception → Traceback.extract | 1.0 | 0.67 | 1.0 | 11 / 5 | $0.088 |
| **평균** | **0.60** | **0.43** | **1.0** | 11 / 4.2 | $0.079 |

## 관찰
1. **기록 13(n=20)과 일관된다**: 도달 65% / 체인 0.60 vs 이번 5개 60% / 0.43. 표본이 작아 체인 값은 흔들리지만 방향은 같다. 엄격 통과 100% 유지.
2. click-01·04 는 13 에서도 미도달이었던 질문이다. 둘 다 도구 7회에서 멈춰 답했다(예산 15 의 절반). quality 모델이 "충분히 알았다" 고 판단하고 끝내는 경우로, 과정 강제 장치(넛지·탐색 상한)가 다루지 않는 실패 유형(읽기는 했으나 체인 끝까지 안 감). 완결성 지표(체인 정확도)가 이걸 잡는다.
3. Experiment 의 쓸모: 질문별 트레이스와 점수가 묶여 있어 click-01 이 어디서 멈췄는지 UI 에서 바로 볼 수 있다. 다음 변경(예: "질문의 끝까지 도달했는지 스스로 점검하는 단계" 추가) 뒤 같은 명령을 돌리면 Compare 가 된다.
4. 20개 전체를 Experiment 로 돌리면 약 $1.7. 로컬 배치 기록 13 이 이미 n=20 수치를 갖고 있어, Experiment 는 변경이 있을 때 5개 단위로 쓰는 것이 비용상 맞다.

## 비용
- $0.39 (중복 실행이면 $0.79). 프로젝트 누적 약 $4.4~4.8.
