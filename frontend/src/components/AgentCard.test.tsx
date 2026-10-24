// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import type { RunResult } from '../types'
import { AgentCard } from './AgentCard'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const result: RunResult = {
  intent: { kind: 'map', reason: '구조' },
  guide: {
    overview: '작은 웹 프레임워크',
    entrypoints: [{ file: 'app/main.py', line: 3, symbol: 'main' }],
    reading_order: [
      { order: 1, file: 'app/main.py', reason: '시작점', ref: { file: 'app/main.py', line: 3, symbol: 'main' } },
      { order: 2, file: 'app/core.py', reason: '핵심 로직', ref: { file: 'app/core.py', line: 99, symbol: null } },
    ],
  },
  answer: null,
  reply: null,
  validation_errors: ['app/core.py:99 — 파일은 50줄까지 → 힌트'],
  thread_id: 't1',
  run_id: null,
}

test('읽기 지도 카드는 진입점 칩·읽기 순서를 그리고, 검증 실패 인용과 꺼진 피드백을 표시한다', () => {
  render(<AgentCard result={result} onCite={() => {}} revealMs={0} />)
  expect(screen.getByText('작은 웹 프레임워크')).toBeTruthy()
  expect(screen.getAllByText('app/main.py:3')).toHaveLength(2) // 진입점 + 1단계
  expect(screen.getByText('app/core.py').tagName).toBe('STRONG')
  const bad = screen.getByRole('button', { name: /app\/core.py:99/ })
  expect(bad.className).toContain('chip-bad')
  expect(bad.getAttribute('title')).toContain('50줄까지')
  expect(screen.getByText('검증 실패 1건')).toBeTruthy()
  const thumbs = screen.getByRole('button', { name: '좋아요' }) as HTMLButtonElement
  expect(thumbs.disabled).toBe(true)
  expect(thumbs.title).toBe('트레이싱 꺼짐')
})

test('다음 단계 카드는 헤더에 N/전체, 파일 경로(굵게)·이유·인용 칩·힌트를 그린다', () => {
  const next: RunResult = {
    ...result,
    intent: { kind: 'chat', reason: '다음 파일' },
    guide: null,
    validation_errors: [],
    next: {
      order: 2,
      file: 'src/requests/sessions.py',
      reason: '세션이 요청을 보낸다',
      ref: { file: 'src/requests/sessions.py', line: 40, symbol: 'Session' },
      remaining: 5,
      hint: '"다음" 이라고 하면 3단계로 넘어갑니다',
    },
  }
  render(<AgentCard result={next} onCite={() => {}} revealMs={0} />)
  expect(screen.getByText('다음 단계 2/7')).toBeTruthy()
  expect(screen.getByText('src/requests/sessions.py').tagName).toBe('STRONG')
  expect(screen.getByText('세션이 요청을 보낸다')).toBeTruthy()
  expect(screen.getByRole('button', { name: /src\/requests\/sessions.py:40/ }).className).toBe('chip')
  expect(screen.getByText('"다음" 이라고 하면 3단계로 넘어갑니다').className).toBe('hint')
})

test('피드백은 성공한 버튼만 .active 로 바뀌고 문구는 그대로이며, 같은 버튼을 다시 눌러도 한 번만 보낸다', async () => {
  const fetchMock = vi.fn(async () => new Response('{"ok":true}', { status: 200 }))
  vi.stubGlobal('fetch', fetchMock)
  render(<AgentCard result={{ ...result, run_id: 'run-1' }} onCite={() => {}} revealMs={0} />)
  const up = screen.getByRole('button', { name: '좋아요' })
  const down = screen.getByRole('button', { name: '별로예요' })
  fireEvent.click(up)
  await waitFor(() => expect(up.className).toContain('active'))
  expect(up.textContent).toBe('👍')
  expect(down.className).toBe('reaction')
  fireEvent.click(up)
  await new Promise((r) => setTimeout(r, 0))
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

const base: RunResult = { ...result, guide: null, validation_errors: [] }

test('이슈 추천 카드는 헤더에 개수·관심사·수준, 이슈마다 번호 링크·제목·난이도·이유·읽을 곳 칩·모듈을 그린다', () => {
  const issues: RunResult = {
    ...base,
    intent: { kind: 'issues', reason: '이슈 추천' },
    issues: {
      repo: 'psf/requests',
      profile: { interests: 'HTTP 쿠키', level: 'beginner' },
      matches: [
        {
          number: 7632,
          title: 'Lingering docstring references',
          url: 'https://github.com/psf/requests/issues/7632',
          labels: [],
          why: '쿠키 관련 문서 수정이라 입문에 맞다',
          modules: ['src.requests.cookies'],
          refs: [{ file: 'src/requests/cookies.py', line: 604, symbol: 'merge_cookies' }],
          difficulty: 'easy',
        },
        {
          number: 7599,
          title: 'Documentation for stream',
          url: 'https://github.com/psf/requests/issues/7599',
          labels: [],
          why: '문서 모호성 개선',
          modules: [],
          refs: [],
          difficulty: 'medium',
        },
      ],
      considered: 5,
      errors: [],
    },
  }
  render(<AgentCard result={issues} onCite={() => {}} revealMs={0} />)
  expect(screen.getByText('이슈 추천 2개 · HTTP 쿠키 · beginner')).toBeTruthy()
  expect(screen.getByRole('link', { name: '#7632' }).getAttribute('href')).toBe('https://github.com/psf/requests/issues/7632')
  expect(screen.getByText('Lingering docstring references')).toBeTruthy()
  expect(screen.getByText('쉬움').className).toContain('badge-easy')
  expect(screen.getByText('보통').className).toContain('badge-medium')
  expect(screen.getByText('쿠키 관련 문서 수정이라 입문에 맞다')).toBeTruthy()
  expect(screen.getByRole('button', { name: /src\/requests\/cookies.py:604/ })).toBeTruthy()
  expect(screen.getByText('src.requests.cookies').className).toBe('modules')
  expect(screen.getAllByText('읽을 곳:')).toHaveLength(1) // 읽을 곳이 없는 이슈는 줄을 그리지 않는다
})

test('PR 리뷰 카드는 PR 번호 링크·요약·바뀐 곳·영향 범위(← via)·관련 테스트·체크포인트를 그린다', () => {
  const onCite = vi.fn()
  const pr: RunResult = {
    ...base,
    intent: { kind: 'pr', reason: 'PR 리뷰' },
    pr_review: {
      repo: 'psf/requests',
      number: 6767,
      title: 'Revert caching a default SSLContext',
      url: 'https://github.com/psf/requests/pull/6767',
      summary: 'SSLContext 캐싱을 되돌린다',
      changed_symbols: [{ name: 'HTTPAdapter.cert_verify', file: 'src/requests/adapters.py', line: 307 }],
      impact: [
        { name: 'HTTPAdapter.send', file: 'src/requests/adapters.py', line: 634, relation: 'caller', via: 'HTTPAdapter.cert_verify' },
      ],
      test_files: ['tests/test_adapters.py'],
      check_points: [
        { text: 'send 가 cert_verify 를 부르는 방식을 확인', refs: [{ file: 'src/requests/adapters.py', line: 634, symbol: 'HTTPAdapter.send' }] },
      ],
      errors: [],
      notes: [],
    },
  }
  render(<AgentCard result={pr} onCite={onCite} revealMs={0} />)
  expect(screen.getByRole('link', { name: '#6767' }).getAttribute('href')).toBe('https://github.com/psf/requests/pull/6767')
  expect(screen.getByText(/Revert caching a default SSLContext/)).toBeTruthy()
  expect(screen.getByText('SSLContext 캐싱을 되돌린다')).toBeTruthy()
  for (const label of ['바뀐 곳', '영향 범위', '호출자', '관련 테스트', '리뷰 체크포인트']) expect(screen.getByText(label)).toBeTruthy()
  expect(screen.getByText('← HTTPAdapter.cert_verify')).toBeTruthy()
  expect(screen.getByText('tests/test_adapters.py')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: /src\/requests\/adapters.py:307/ }))
  expect(onCite).toHaveBeenCalledWith({ file: 'src/requests/adapters.py', line: 307, symbol: 'HTTPAdapter.cert_verify' })
  expect(screen.getAllByRole('button', { name: /src\/requests\/adapters.py:634/ })).toHaveLength(2) // 영향 범위 + 체크포인트
})

test('프로필 질문(ask_user)은 "질문" 카드에 질문 상자와 답하는 법을 그린다', () => {
  const ask: RunResult = { ...base, intent: { kind: 'issues', reason: '이슈 추천' }, reply: '관심 영역과 수준을 알려 주세요', ask_user: true }
  render(<AgentCard result={ask} onCite={() => {}} revealMs={0} />)
  expect(screen.getByText('질문')).toBeTruthy()
  expect(screen.getByText('관심 영역과 수준을 알려 주세요').className).toContain('ask-box')
  expect(screen.getByText(/다음 메시지로 답해 주세요/)).toBeTruthy()
})
