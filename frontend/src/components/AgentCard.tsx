// 에이전트 응답 카드: 읽기 지도 | 따라가기 | 다음 단계 | 안내. 헤더 띠 + 본문, 블록을 하나씩 드러낸다.
import { useEffect, useRef, useState } from 'react'
import { sendFeedback } from '../lib/api'
import type { CodeRef, RunResult } from '../types'
import { CitationChip } from './CitationChip'

interface Props {
  result: RunResult
  onCite: (ref: CodeRef) => void
  revealMs?: number // 0 이면 한 번에 (테스트용)
}

// 검증 오류 문구는 "file:line — 이유 → 힌트" 형식이다.
const errorFor = (errors: string[], ref: CodeRef) =>
  errors.find((e) => e.startsWith(`${ref.file}:${ref.line} `))

function blockCount(r: RunResult): number {
  if (r.guide) return 2 + r.guide.reading_order.length // 개요 + 진입점 + 단계들
  if (r.answer) return r.answer.steps.length + 1 // 단계들 + 요약
  return 1
}

// 헤더 띠 제목. 다음 단계의 전체 = 현재 순번 + 남은 단계 수
function cardTitle(r: RunResult): string {
  if (r.guide) return '읽기 지도'
  if (r.answer) return '따라가기'
  if (r.next) return `다음 단계 ${r.next.order}/${r.next.order + r.next.remaining}`
  return '안내'
}

export function AgentCard({ result, onCite, revealMs = 120 }: Props) {
  const total = blockCount(result)
  const [shown, setShown] = useState(revealMs ? 1 : total)
  const errors = result.validation_errors
  const chip = (ref: CodeRef, key: string | number) => (
    <CitationChip key={key} codeRef={ref} error={errorFor(errors, ref)} onOpen={onCite} />
  )

  useEffect(() => {
    if (!revealMs || shown >= total) return
    const t = setTimeout(() => setShown((n) => n + 1), revealMs)
    return () => clearTimeout(t)
  }, [shown, total, revealMs])

  const { guide, answer } = result
  const next = !guide && !answer ? result.next : null
  return (
    <article className="card">
      <header className="card-head">{cardTitle(result)}</header>
      <div className="card-body">
        {guide && (
          <>
            <p className="overview fade">{guide.overview}</p>
            {shown > 1 && (
              <section className="fade">
                <h3 className="label">진입점</h3>
                <div className="chips">{guide.entrypoints.map((r, i) => chip(r, i))}</div>
              </section>
            )}
            {shown > 2 && <h3 className="label fade">읽기 순서</h3>}
            <ol className="steps">
              {guide.reading_order.slice(0, Math.max(0, shown - 2)).map((s) => (
                <li key={s.order} className="step fade">
                  <strong className="step-file">{s.file}</strong>
                  <p>{s.reason}</p>
                  <div className="chips">{chip(s.ref, 0)}</div>
                </li>
              ))}
            </ol>
          </>
        )}
        {answer && (
          <>
            <ol className="steps">
              {answer.steps.slice(0, shown).map((s) => (
                <li key={s.order} className="step fade">
                  <strong>{s.title}</strong>
                  <p>{s.explanation}</p>
                  <div className="chips">{s.refs.map((r, i) => chip(r, i))}</div>
                </li>
              ))}
            </ol>
            {shown > answer.steps.length && <p className="summary fade">{answer.summary}</p>}
          </>
        )}
        {next && (
          <section className="next fade">
            <strong className="step-file">{next.file}</strong>
            <p>{next.reason}</p>
            <div className="chips">{chip(next.ref, 0)}</div>
            {next.hint && <p className="hint">{next.hint}</p>}
          </section>
        )}
        {!guide && !answer && !next && <p className="reply fade">{result.reply}</p>}
        {shown >= total && (
          <>
            {errors.length > 0 && (
              <details className="errors">
                <summary>검증 실패 {errors.length}건</summary>
                <ul>{errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
              </details>
            )}
            <Feedback runId={result.run_id} />
          </>
        )}
      </div>
    </article>
  )
}

// 👍 👎 인용 오류 → POST /feedback (GitHub 리액션 같은 작은 pill). run_id 가 없으면(트레이싱 꺼짐) 비활성.
// 보내기에 성공한 버튼만 활성 색(.active)으로 바뀌고, 같은 버튼을 다시 눌러도 다시 보내지 않는다. 실패만 문구로 알린다.
const REACTIONS = [
  { id: 'up', text: '👍', aria: '좋아요', score: 1, kind: 'thumbs' },
  { id: 'down', text: '👎', aria: '별로예요', score: 0, kind: 'thumbs' },
  { id: 'cite', text: '인용 오류', aria: '인용 오류 신고', score: 0, kind: 'citation_error' },
] as const

function Feedback({ runId }: { runId: string | null }) {
  const [sent, setSent] = useState<string[]>([]) // 보내기에 성공한 버튼 id
  const [error, setError] = useState('')
  const pending = useRef(new Set<string>()) // 응답 오기 전 연타 방지
  const title = runId ? undefined : '트레이싱 꺼짐'
  const send = (r: (typeof REACTIONS)[number]) => {
    if (!runId || sent.includes(r.id) || pending.current.has(r.id)) return
    pending.current.add(r.id)
    sendFeedback(runId, r.score, r.kind)
      .then(
        () => {
          setSent((s) => [...s, r.id])
          setError('')
        },
        (e: Error) => setError(`보내지 못함: ${e.message}`), // 실패면 다시 누를 수 있다
      )
      .finally(() => pending.current.delete(r.id))
  }
  return (
    <div className="feedback">
      {REACTIONS.map((r) => {
        const on = sent.includes(r.id)
        return (
          <button
            key={r.id}
            type="button"
            className={on ? 'reaction active' : 'reaction'}
            disabled={!runId}
            title={title}
            aria-label={r.aria}
            aria-pressed={on}
            onClick={() => send(r)}
          >
            {r.text}
          </button>
        )
      })}
      {error && <span className="inline-error feedback-error">{error}</span>}
    </div>
  )
}
