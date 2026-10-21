import { expect, test } from 'vitest'
import type { SupervisorEvent } from '../types'
import { describeEvent } from './events'

test('Supervisor 이벤트 6종을 각각 한국어 한 줄로 바꾼다', () => {
  const events: SupervisorEvent[] = [
    { type: 'IndexReady', repo_id: 'a/b@1234567', cached: true, files: 20 },
    { type: 'IntentDecided', kind: 'map', reason: '구조 질문' },
    { type: 'GateApplied', from: 'ask', to: 'map', reason: '이름 없음' },
    { type: 'GuideReady', steps: 7, errors: 0 },
    { type: 'AnswerReady', steps: 3, errors: 1 },
    { type: 'Replied' },
  ]
  expect(events.map(describeEvent)).toEqual([
    '인덱스 준비 · cache hit · 파일 20개',
    '의도: map — 구조 질문',
    '게이트: ask → map',
    '읽기 지도 생성 완료 (7단계)',
    '따라가기 답 완성 (3단계, 검증 실패 1건)',
    '안내 답변',
  ])
})

test('NextStep 이벤트는 "다음 단계 N (남은 M개)" 로 바꾼다', () => {
  expect(describeEvent({ type: 'NextStep', order: 2, remaining: 5 })).toBe('다음 단계 2 (남은 5개)')
})

test('next 의도는 "다음 단계" 로 보이고 게이트 문장도 next 를 그 이름으로 쓴다', () => {
  expect(describeEvent({ type: 'IntentDecided', kind: 'next', reason: '이어 읽기' })).toBe('의도: 다음 단계 — 이어 읽기')
  expect(describeEvent({ type: 'GateApplied', from: 'map', to: 'next', reason: '지도 있음' })).toBe('게이트: map → 다음 단계')
  expect(describeEvent({ type: 'GateApplied', from: 'next', to: 'map', reason: '지도 없음' })).toBe('게이트: 다음 단계 → map')
})
