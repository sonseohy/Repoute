// 에이전트 응답 카드: 읽기 지도 | 따라가기 | 다음 단계 | 이슈 추천 | PR 리뷰 | 질문 | 안내.
// 헤더 띠 + 본문, 블록을 하나씩 드러낸다.
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { sendFeedback } from '../lib/api'
import type { CodeRef, IssueMatches, PRReview, RunResult, SymbolRef } from '../types'
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

const LEVEL_LABEL: Record<string, string> = { beginner: 'beginner', intermediate: 'intermediate', advanced: 'advanced' }
const DIFFICULTY_LABEL: Record<string, string> = { easy: '쉬움', medium: '보통', hard: '어려움' }

// 심볼 위치(PR Coach) → 인용 칩이 받는 CodeRef
const symbolRef = (s: SymbolRef): CodeRef => ({ file: s.file, line: s.line, symbol: s.name })

// 헤더 띠 제목. 다음 단계의 전체 = 현재 순번 + 남은 단계 수
function cardTitle(r: RunResult): ReactNode {
  if (r.guide) return '읽기 지도'
  if (r.answer) return '따라가기'
  if (r.next) return `다음 단계 ${r.next.order}/${r.next.order + r.next.remaining}`
  if (r.issues) {
    const { matches, profile } = r.issues
    const interests = profile.interests ? ` · ${profile.interests}` : ''
    return `이슈 추천 ${matches.length}개${interests} · ${LEVEL_LABEL[profile.level] ?? profile.level}`
  }
  if (r.pr_review) {
    const pr = r.pr_review
    return (
      <>
        PR{' '}
        <a href={pr.url} target="_blank" rel="noreferrer" className="pr-num">
          #{pr.number}
        </a>{' '}
        리뷰 · {pr.title}
      </>
    )
  }
  if (r.ask_user) return '질문'
  return '안내'
}

// 이슈 추천: 이슈마다 번호·제목·난이도 → 추천 이유 → 읽을 곳(인용 칩) → 관련 모듈
function IssueList({ data, chip }: { data: IssueMatches; chip: (ref: CodeRef, key: string | number) => ReactNode }) {
  if (data.matches.length === 0) {
    return <p className="reply fade">추천할 이슈를 찾지 못했습니다. (후보 {data.considered}개)</p>
  }
  return (
    <ol className="issues">
      {data.matches.map((m) => (
        <li key={m.number} className="issue fade">
          <div className="issue-head">
            <a href={m.url} target="_blank" rel="noreferrer" className="issue-num">
              #{m.number}
            </a>
            <strong className="issue-title">{m.title}</strong>
            <span className={`badge badge-${m.difficulty}`}>{DIFFICULTY_LABEL[m.difficulty] ?? m.difficulty}</span>
          </div>
          <p>{m.why}</p>
          {m.refs.length > 0 && (
            <div className="chips">
              <span className="chips-label">읽을 곳:</span>
              {m.refs.map((r, i) => chip(r, i))}
            </div>
          )}
          {m.modules.length > 0 && <p className="modules">{m.modules.join(' · ')}</p>}
        </li>
      ))}
    </ol>
  )
}

// PR 리뷰: 요약 → 바뀐 곳 → 영향 범위(호출자·피호출자) → 관련 테스트 → 리뷰 체크포인트
function PrReviewBody({ pr, chip }: { pr: PRReview; chip: (ref: CodeRef, key: string | number) => ReactNode }) {
  const callers = pr.impact.filter((s) => s.relation === 'caller')
  const callees = pr.impact.filter((s) => s.relation === 'callee')
  const impactRows = (rows: typeof pr.impact) =>
    rows.map((s, i) => (
      <div key={i} className="impact-row">
        {chip(symbolRef(s), 0)}
        <span className="via">← {s.via}</span>
      </div>
    ))
  return (
    <>
      <p className="overview fade">{pr.summary}</p>
      {pr.changed_symbols.length > 0 && (
        <section className="fade">
          <h3 className="label">바뀐 곳</h3>
          <div className="chips chips-col">{pr.changed_symbols.map((s, i) => chip(symbolRef(s), i))}</div>
        </section>
      )}
      {pr.impact.length > 0 && (
        <section className="fade">
          <h3 className="label">영향 범위</h3>
          {callers.length > 0 && (
            <>
              <strong className="sub-label">호출자</strong>
              {impactRows(callers)}
            </>
          )}
          {callees.length > 0 && (
            <>
              <strong className="sub-label">피호출자</strong>
              {impactRows(callees)}
            </>
          )}
        </section>
      )}
      {pr.test_files.length > 0 && (
        <section className="fade">
          <h3 className="label">관련 테스트</h3>
          <ul className="test-files">
            {pr.test_files.map((f) => (
              <li key={f}>{f}</li>
            ))}
          </ul>
        </section>
      )}
      {pr.check_points.length > 0 && (
        <section className="fade">
          <h3 className="label">리뷰 체크포인트</h3>
          {pr.check_points.map((c, i) => (
            <div key={i} className="checkpoint">
              <p>{c.text}</p>
              {c.refs.length > 0 && <div className="chips">{c.refs.map((r, j) => chip(r, j))}</div>}
            </div>
          ))}
        </section>
      )}
      {[...pr.notes, ...pr.errors].map((n, i) => (
        <p key={i} className="hint">
          {n}
        </p>
      ))}
    </>
  )
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
  const issues = !guide && !answer && !next ? result.issues : null
  const pr = !guide && !answer && !next && !issues ? result.pr_review : null
  const plain = !guide && !answer && !next && !issues && !pr
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
        {issues && <IssueList data={issues} chip={chip} />}
        {pr && <PrReviewBody pr={pr} chip={chip} />}
        {plain && result.ask_user && (
          <>
            <p className="ask-box fade">{result.reply}</p>
            <p className="hint">다음 메시지로 답해 주세요 · 예: 인증, beginner</p>
          </>
        )}
        {plain && !result.ask_user && <p className="reply fade">{result.reply}</p>}
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
