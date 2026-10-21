// 화면 하나: 상단 레포 바 · 가운데 대화 · 하단 입력 · 오른쪽 코드 드로어.
import { useCallback, useEffect, useRef, useState } from 'react'
import { CodeDrawer } from './components/CodeDrawer'
import { Composer } from './components/Composer'
import { LogoMark } from './components/LogoMark'
import { MessageList } from './components/MessageList'
import { RepoBar } from './components/RepoBar'
import { runThread } from './lib/api'
import { describeEvent } from './lib/events'
import type { CodeRef, Message, RepoSummary, RunResult, UpdateData } from './types'

type AgentPatch = Partial<Extract<Message, { role: 'agent' }>>

const EXAMPLES = ['이 레포 어디서부터 읽으면 돼?', '진입점 함수를 호출하면 내부에서 무슨 일이 일어나?']

export default function App() {
  const [repo, setRepo] = useState<{ summary: RepoSummary; url: string } | null>(null)
  const [threadId, setThreadId] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [busy, setBusy] = useState(false)
  const [drawer, setDrawer] = useState<CodeRef | null>(null)
  const bottom = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottom.current?.scrollIntoView?.({ behavior: 'smooth', block: 'end' })
  }, [messages])

  // 레포가 바뀌면 새 스레드 (같은 레포 안의 후속 질문은 thread_id 로 이어진다)
  function onIndexed(summary: RepoSummary, url: string) {
    setRepo({ summary, url })
    setThreadId(crypto.randomUUID())
    setMessages([])
    setDrawer(null)
  }

  const patch = (id: string, change: (m: Extract<Message, { role: 'agent' }>) => AgentPatch) =>
    setMessages((ms) => ms.map((m) => (m.id === id && m.role === 'agent' ? { ...m, ...change(m) } : m)))

  async function send(text: string) {
    if (!repo || busy) return
    const id = crypto.randomUUID()
    setMessages((ms) => [
      ...ms,
      { role: 'user', id: `${id}-q`, text },
      { role: 'agent', id, lines: [], pending: true },
    ])
    await run(id, text)
  }

  // 에이전트 메시지 id 하나를 실행해 진행 줄·결과·오류를 채운다.
  async function run(id: string, text: string) {
    if (!repo) return
    setBusy(true)
    patch(id, () => ({ lines: [], error: undefined, pending: true }))
    try {
      await runThread(threadId, repo.url, text, ({ event, data }) => {
        if (event === 'update') {
          const lines = (data as UpdateData).events.map(describeEvent)
          patch(id, (m) => ({ lines: [...m.lines, ...lines] }))
        } else if (event === 'result') patch(id, () => ({ result: data as RunResult }))
        else if (event === 'error') patch(id, () => ({ error: (data as { message: string }).message || '알 수 없는 오류' }))
      })
    } catch (e) {
      patch(id, () => ({ error: e instanceof Error ? e.message : String(e) }))
    } finally {
      patch(id, () => ({ pending: false }))
      setBusy(false)
    }
  }

  const closeDrawer = useCallback(() => setDrawer(null), [])

  return (
    <div className="app">
      <RepoBar repo={repo?.summary ?? null} onIndexed={onIndexed} />
      <main className="main">
        <div className="column">
          {messages.length === 0 && (
            <div className="blankslate">
              <span className="blankslate-icon"><LogoMark size={24} /></span>
              <h1>코드 읽기를 안내해 드릴게요</h1>
              <p className="muted">{repo ? '읽기 지도나 특정 함수의 흐름을 물어보세요.' : '먼저 위에 GitHub 레포 URL 을 넣어 주세요.'}</p>
              {repo && (
                <div className="examples">
                  {EXAMPLES.map((q) => (
                    <button key={q} type="button" className="btn" onClick={() => send(q)}>{q}</button>
                  ))}
                </div>
              )}
            </div>
          )}
          <MessageList messages={messages} onCite={setDrawer} />
          <div ref={bottom} />
        </div>
      </main>
      <Composer
        disabled={!repo || busy}
        hint={repo ? '질문을 입력하세요 (Enter 전송, Shift+Enter 줄바꿈)' : '레포를 먼저 불러오세요'}
        onSend={send}
      />
      <CodeDrawer repoId={repo?.summary.repo_id ?? null} target={drawer} onClose={closeDrawer} />
    </div>
  )
}
