// 대화 목록(GitHub 코멘트 타임라인 느낌): 왼쪽 아바타 + 오른쪽 내용. 사용자는 텍스트, 에이전트는 진행 줄 + 카드.
import type { CodeRef, Message } from '../types'
import { AgentCard } from './AgentCard'
import { LogoMark } from './LogoMark'
import { TypingIndicator } from './TypingIndicator'

interface Props {
  messages: Message[]
  onCite: (ref: CodeRef) => void
}

export function MessageList({ messages, onCite }: Props) {
  return (
    <div className="messages" aria-live="polite">
      {messages.map((m) =>
        m.role === 'user' ? (
          <div key={m.id} className="msg msg-user">
            <span className="avatar avatar-user" aria-hidden="true">U</span>
            <div className="msg-body msg-text">{m.text}</div>
          </div>
        ) : (
          <div key={m.id} className="msg msg-agent">
            <span className="avatar avatar-agent" aria-hidden="true"><LogoMark size={16} /></span>
            <div className="msg-body">
              {m.result ? (
                <details className="progress-done">
                  <summary>진행 기록 {m.lines.length}줄</summary>
                  <Progress lines={m.lines} pending={false} />
                </details>
              ) : (
                <Progress lines={m.lines} pending={m.pending} />
              )}
              {m.pending && <TypingIndicator />}
              {m.error && <div className="inline-error" role="alert">{m.error}</div>}
              {m.result && <AgentCard result={m.result} onCite={onCite} />}
            </div>
          </div>
        ),
      )}
    </div>
  )
}

// 끝난 줄은 녹색 점, 진행 중인 마지막 줄은 회색 점
function Progress({ lines, pending }: { lines: string[]; pending: boolean }) {
  return (
    <ul className="progress">
      {lines.map((line, i) => (
        <li key={i} className={pending && i === lines.length - 1 ? 'fade' : 'fade ok'}>{line}</li>
      ))}
    </ul>
  )
}
