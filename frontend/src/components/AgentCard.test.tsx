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
