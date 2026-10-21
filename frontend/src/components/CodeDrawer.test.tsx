// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { CodeDrawer } from './CodeDrawer'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })

test('감싸는 심볼이 있으면 그 범위를 받아 헤더에 kind name · file:start-end 를 찍고, docstring 과 설명 패널을 그린다', async () => {
  const fetchMock = vi.fn(async (input: string | URL | Request) => {
    const url = new URL(String(input))
    if (url.pathname.endsWith('/symbol'))
      return ok({ name: 'run', kind: 'function', file: 'app/run.py', line_start: 10, line_end: 12, docstring: '앱을 실행한다.' })
    if (url.pathname.endsWith('/files')) {
      const start = Number(url.searchParams.get('start'))
      const end = Number(url.searchParams.get('end'))
      const lines = Array.from({ length: end - start + 1 }, (_, i) => ({ n: start + i, text: `line ${start + i}` }))
      return ok({ path: 'app/run.py', start, end, lines })
    }
    if (url.pathname.endsWith('/explain'))
      return ok({ name: 'run', kind: 'function', line_start: 10, line_end: 12, what: '설정을 읽고 서버를 띄운다.', watch: ['포트 기본값 (줄 11)'], cached: true })
    return new Response('{}', { status: 404 })
  })
  vi.stubGlobal('fetch', fetchMock)

  const { container } = render(
    <CodeDrawer repoId="o/r@abc" target={{ file: 'app/run.py', line: 11, symbol: 'run' }} onClose={() => {}} />,
  )

  expect(await screen.findByText('app/run.py:10-12')).toBeTruthy()
  expect(screen.getByText('function').className).toBe('drawer-kind')
  expect(screen.getByText('앱을 실행한다.').className).toBe('docstring')
  expect(screen.getByRole('heading', { name: /docstring/ })).toBeTruthy()
  // docstring 이 있으면 what 은 따로 보여 주지 않는다 (중복)
  expect(await screen.findByRole('button', { name: '(줄 11)' })).toBeTruthy()
  expect(screen.queryByText('설정을 읽고 서버를 띄운다.')).toBeNull()
  expect(screen.getByRole('button', { name: '(줄 11)' }).className).toBe('line-link')
  expect(screen.queryByText('캐시')).toBeNull() // 캐시 여부는 표시하지 않는다
  // 심볼 범위(10~12)만 받았고, 인용 줄(11)이 하이라이트된다
  const files = fetchMock.mock.calls.map(([u]) => new URL(String(u))).find((u) => u.pathname.endsWith('/files'))!
  expect([files.searchParams.get('start'), files.searchParams.get('end')]).toEqual(['10', '12'])
  expect(container.querySelector('.ln.hit')?.textContent).toContain('line 11')
  expect(screen.queryByText('다음 200줄 보기')).toBeNull()
})

test('설명에 docstring_ko 가 오면 한국어 요약을 보여 주고 원문 docstring 은 "원문 보기" 안에 접어 둔다', async () => {
  const fetchMock = vi.fn(async (input: string | URL | Request) => {
    const url = new URL(String(input))
    if (url.pathname.endsWith('/symbol'))
      return ok({ name: 'run', kind: 'function', file: 'app/run.py', line_start: 10, line_end: 12, docstring: 'Run the app.' })
    if (url.pathname.endsWith('/files')) {
      const start = Number(url.searchParams.get('start'))
      const end = Number(url.searchParams.get('end'))
      const lines = Array.from({ length: end - start + 1 }, (_, i) => ({ n: start + i, text: `line ${start + i}` }))
      return ok({ path: 'app/run.py', start, end, lines })
    }
    if (url.pathname.endsWith('/explain'))
      return ok({
        name: 'run', kind: 'function', line_start: 10, line_end: 12,
        what: '서버를 띄운다.', watch: [], docstring_ko: '앱을 실행한다.', cached: false,
      })
    return new Response('{}', { status: 404 })
  })
  vi.stubGlobal('fetch', fetchMock)

  const { container } = render(
    <CodeDrawer repoId="o/r@abc2" target={{ file: 'app/run.py', line: 11, symbol: 'run' }} onClose={() => {}} />,
  )

  // 응답 전에는 원문 docstring 을 바로 보여 준다
  expect((await screen.findByText('Run the app.')).className).toBe('docstring')
  // 응답 후에는 한국어 요약이 그 자리에 오고, 원문은 접힌 details 안에 있다
  expect((await screen.findByText('앱을 실행한다.')).className).toBe('docstring docstring-ko')
  const details = container.querySelector('details.docstring-src') as HTMLDetailsElement
  expect(details.open).toBe(false)
  expect(details.querySelector('summary')?.textContent).toBe('원문 보기')
  expect(details.querySelector('pre.docstring')?.textContent).toBe('Run the app.')
})

test('원문 docstring 이 없는 심볼은 제목이 "코드 요약" 이고 코드를 읽고 쓴 요약(what)을 그 자리에 둔다', async () => {
  const fetchMock = vi.fn(async (input: string | URL | Request) => {
    const url = new URL(String(input))
    if (url.pathname.endsWith('/symbol'))
      return ok({ name: 'run', kind: 'function', file: 'app/run.py', line_start: 10, line_end: 12, docstring: null })
    if (url.pathname.endsWith('/files')) {
      const start = Number(url.searchParams.get('start'))
      const end = Number(url.searchParams.get('end'))
      const lines = Array.from({ length: end - start + 1 }, (_, i) => ({ n: start + i, text: `line ${start + i}` }))
      return ok({ path: 'app/run.py', start, end, lines })
    }
    if (url.pathname.endsWith('/explain'))
      return ok({
        name: 'run', kind: 'function', line_start: 10, line_end: 12,
        what: '서버를 띄운다.', watch: [], docstring_ko: '', cached: false,
      })
    return new Response('{}', { status: 404 })
  })
  vi.stubGlobal('fetch', fetchMock)

  const { container } = render(
    <CodeDrawer repoId="o/r@abc3" target={{ file: 'app/run.py', line: 11, symbol: 'run' }} onClose={() => {}} />,
  )

  expect((await screen.findByText('서버를 띄운다.')).className).toBe('docstring docstring-ko')
  expect(screen.getByRole('heading', { name: '코드 요약' })).toBeTruthy()
  expect(screen.queryByRole('heading', { name: 'docstring' })).toBeNull()
  expect(container.querySelector('details.docstring-src')).toBeNull()
})
