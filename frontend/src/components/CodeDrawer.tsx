// 인용 칩을 누르면 오른쪽에서 나오는 코드 보기 (GitHub 코드 뷰처럼 줄 번호 + 노란 하이라이트, 구문 강조 없음).
// 인용 줄을 감싸는 심볼(함수·클래스) 전체를 보여 주고, 위에 docstring(한국어 요약) · 꼭 봐야 할 코드를 붙인다.
// docstring 은 응답 전엔 원문을 바로 보여 주고, 응답에 한국어 요약(docstring_ko)이 오면 그걸로 바꾸고 원문은 접어 둔다.
// 원문 docstring 이 없는 심볼은 제목을 '코드 요약' 으로 바꾸고 코드를 읽고 쓴 요약(what)을 같은 자리에 둔다.
// 감싸는 심볼이 없으면 예전처럼 인용 줄 ±15줄.
import { useEffect, useRef, useState } from 'react'
import { explainSymbol, getSymbol, readFile } from '../lib/api'
import { parseLineRefs } from '../lib/lineRefs'
import type { CodeRef, Explanation, FileSlice, SymbolSpan } from '../types'

const CONTEXT = 15
const CHUNK = 200 // /files 한 번에 받을 수 있는 최대 줄 수

interface Code {
  symbol: SymbolSpan | null
  lines: FileSlice['lines']
  end: number // 지금까지 받은 마지막 줄
}

// 세션 메모리 캐시 (컴포넌트 밖) — 같은 인용·같은 심볼을 다시 열면 재요청하지 않는다.
const codeCache = new Map<string, Code>() // `${repoId}|file:line` → 받은 코드
const explainCache = new Map<string, Promise<Explanation | null>>() // `${repoId}|file:line_start` → 설명 (진행 중인 요청도 공유)

// 심볼 끝(없으면 지금 끝)을 넘지 않게, upTo 줄까지 200줄씩 이어 받는다.
async function extend(repoId: string, file: string, code: Code, upTo: number): Promise<Code> {
  const goal = Math.min(upTo, code.symbol ? code.symbol.line_end : code.end)
  let { lines, end } = code
  while (end < goal) {
    const s = await readFile(repoId, file, end + 1, Math.min(goal, end + CHUNK))
    if (s.lines.length === 0 || s.end <= end) break // 파일 끝
    lines = [...lines, ...s.lines]
    end = s.end
  }
  return { ...code, lines, end }
}

async function loadCode(repoId: string, t: CodeRef, cacheKey: string): Promise<Code> {
  const hit = codeCache.get(cacheKey)
  if (hit) return hit
  let symbol: SymbolSpan | null = null
  let cacheable = true
  try {
    symbol = await getSymbol(repoId, t.file, t.line)
  } catch {
    cacheable = false // 심볼 조회가 일시적으로 실패하면 ±15줄로 보여 주되 캐시하지 않는다
  }
  let code: Code
  if (symbol) {
    const s = await readFile(repoId, t.file, symbol.line_start, Math.min(symbol.line_end, symbol.line_start + CHUNK - 1))
    // 인용 줄이 첫 200줄 밖이면 거기까지 이어 받는다 (하이라이트가 보이도록)
    code = await extend(repoId, t.file, { symbol, lines: s.lines, end: s.end }, t.line + CONTEXT)
  } else {
    const s = await readFile(repoId, t.file, Math.max(1, t.line - CONTEXT), t.line + CONTEXT)
    code = { symbol: null, lines: s.lines, end: s.end }
  }
  if (cacheable) codeCache.set(cacheKey, code)
  return code
}

function loadExplanation(repoId: string, t: CodeRef, symbol: SymbolSpan): Promise<Explanation | null> {
  const k = `${repoId}|${symbol.file}:${symbol.line_start}`
  let p = explainCache.get(k)
  if (!p) {
    p = explainSymbol(repoId, t.file, t.line)
    p.catch(() => explainCache.delete(k)) // 실패는 캐시하지 않는다 → "다시 시도" 가능
    explainCache.set(k, p)
  }
  return p
}

interface Props {
  repoId: string | null
  target: CodeRef | null
  onClose: () => void
}

export function CodeDrawer({ repoId, target, onClose }: Props) {
  // 어떤 인용(key)의 응답인지 같이 둔다 → 다른 칩을 누르면 이전 내용은 자동으로 안 보인다
  const [loaded, setLoaded] = useState<{ key: string; code?: Code; error?: string }>({ key: '' })
  const [explain, setExplain] = useState<{ key: string; data?: Explanation | null; failed?: boolean }>({ key: '' })
  const [attempt, setAttempt] = useState(0) // "다시 시도" 횟수
  const [more, setMore] = useState(false) // 이어 받는 중
  const [flash, setFlash] = useState<{ key: string; start: number; end: number } | null>(null)
  const hit = useRef<HTMLDivElement>(null)
  const pre = useRef<HTMLPreElement>(null)
  const closeBtn = useRef<HTMLButtonElement>(null)
  const label = target ? `${target.file}:${target.line}` : ''
  const key = target && repoId ? `${repoId}|${label}` : ''
  const current = loaded.key === key ? loaded : null
  const code = current?.code
  const error = current?.error
  const symbol = code?.symbol ?? null
  const explainKey = `${key}#${attempt}`
  const exp = explain.key === explainKey ? explain : null

  useEffect(() => {
    if (!target || !repoId) return
    const k = `${repoId}|${target.file}:${target.line}`
    let alive = true // 늦게 온 이전 응답은 버린다
    loadCode(repoId, target, k).then(
      (c) => alive && setLoaded({ key: k, code: c }),
      (e: Error) => alive && setLoaded({ key: k, error: e.message }),
    )
    closeBtn.current?.focus()
    return () => {
      alive = false
    }
  }, [repoId, target])

  // 심볼이 정해지면 설명을 한 번 부른다 (캐시에 있으면 그대로)
  useEffect(() => {
    if (!target || !repoId || !symbol) return
    const k = `${repoId}|${target.file}:${target.line}#${attempt}`
    let alive = true
    loadExplanation(repoId, target, symbol).then(
      (e) => alive && setExplain({ key: k, data: e }),
      () => alive && setExplain({ key: k, failed: true }),
    )
    return () => {
      alive = false
    }
  }, [repoId, target, symbol, attempt])

  // 처음 받았을 때만 인용 줄로 스크롤 (이어 받기로는 움직이지 않는다)
  const loadedKey = code ? key : ''
  useEffect(() => {
    if (loadedKey) hit.current?.scrollIntoView?.({ block: 'center' })
  }, [loadedKey])

  // 줄 링크: 그 줄로 스크롤 + 잠깐 하이라이트
  useEffect(() => {
    if (!flash) return
    pre.current?.querySelector(`[data-n="${flash.start}"]`)?.scrollIntoView?.({ block: 'center', behavior: 'smooth' })
    const t = setTimeout(() => setFlash(null), 1600)
    return () => clearTimeout(t)
  }, [flash])

  useEffect(() => {
    if (!target) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [target, onClose])

  // upTo 줄까지 이어 받아 아래에 붙인다 (캐시도 갱신)
  async function grow(upTo: number) {
    if (!repoId || !target || !code) return
    const k = key
    setMore(true)
    try {
      const next = await extend(repoId, target.file, code, upTo)
      codeCache.set(k, next)
      setLoaded((l) => (l.key === k ? { key: k, code: next } : l))
    } catch (e) {
      setLoaded((l) => (l.key === k ? { ...l, error: (e as Error).message } : l))
    } finally {
      setMore(false)
    }
  }

  async function jump(start: number, end: number) {
    if (code && end > code.end) await grow(end)
    setFlash({ key, start, end })
  }

  const isHit = (n: number) => n === target?.line
  const isFlash = (n: number) => !!flash && flash.key === key && n >= flash.start && n <= flash.end
  const showExplain = !!symbol && (symbol.docstring || exp?.data !== null) // 404(설명 없음)이고 docstring 도 없으면 숨김

  return (
    <aside className={target ? 'drawer open' : 'drawer'} aria-hidden={!target} aria-label="코드 보기">
      <div className="drawer-head">
        {symbol ? (
          <div className="drawer-heading">
            <span className="drawer-title">
              <span className="drawer-kind">{symbol.kind}</span> {symbol.name}
            </span>
            <span className="drawer-loc">{`${symbol.file}:${symbol.line_start}-${symbol.line_end}`}</span>
          </div>
        ) : (
          <span className="drawer-title">{label}</span>
        )}
        <button ref={closeBtn} type="button" className="btn btn-sm" onClick={onClose} aria-label="코드 보기 닫기" tabIndex={target ? 0 : -1}>
          ✕
        </button>
      </div>
      {showExplain && (
        <section className="explain" aria-label="코드 설명">
          <h3 className="label">{symbol.docstring ? 'docstring' : '코드 요약'}</h3>
          {symbol.docstring ? (
            exp?.data?.docstring_ko ? (
              <>
                <p className="docstring docstring-ko">{exp.data.docstring_ko}</p>
                <details className="docstring-src">
                  <summary>원문 보기</summary>
                  <pre className="docstring">{symbol.docstring}</pre>
                </details>
              </>
            ) : (
              <pre className="docstring">{symbol.docstring}</pre>
            )
          ) : (
            exp?.data && <p className="docstring docstring-ko">{exp.data.what}</p>
          )}
          {!exp && (
            <div className="explain-loading" role="status">
              <span className="typing" aria-hidden="true"><span /><span /><span /></span>
              설명 준비 중…
            </div>
          )}
          {exp?.failed && (
            <div className="explain-failed">
              <span className="inline-error">설명을 만들지 못했습니다</span>
              <button type="button" className="btn btn-sm" onClick={() => setAttempt((n) => n + 1)}>다시 시도</button>
            </div>
          )}
          {exp?.data && (
            <>
              {exp.data.watch.length > 0 && (
                <>
                  <h3 className="label">꼭 봐야 할 코드</h3>
                  <ul className="explain-watch">
                    {exp.data.watch.map((w, i) => (
                      <li key={i}>
                        {parseLineRefs(w).map((p, j) =>
                          typeof p === 'string' ? (
                            <span key={j}>{p}</span>
                          ) : (
                            <button key={j} type="button" className="line-link" onClick={() => jump(p.start, p.end)}>
                              {p.text}
                            </button>
                          ),
                        )}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </>
          )}
        </section>
      )}
      <div className="drawer-body">
        {error && <div className="inline-error pad" role="alert">{error}</div>}
        {!code && !error && target && <div className="muted pad">불러오는 중…</div>}
        {code && (
          <pre className="code" ref={pre}>
            {code.lines.map((l) => (
              <div
                key={l.n}
                data-n={l.n}
                ref={isHit(l.n) ? hit : undefined}
                className={'ln' + (isHit(l.n) ? ' hit' : '') + (isFlash(l.n) ? ' flash' : '')}
              >
                <span className="ln-no">{l.n}</span>
                <span className="ln-text">{l.text || ' '}</span>
              </div>
            ))}
          </pre>
        )}
        {symbol && code && code.end < symbol.line_end && (
          <div className="pad more">
            <button type="button" className="btn btn-sm" disabled={more} onClick={() => grow(code.end + CHUNK)}>
              {more ? '불러오는 중…' : '다음 200줄 보기'}
            </button>
          </div>
        )}
      </div>
    </aside>
  )
}
