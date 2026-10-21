// 백엔드 호출 래퍼. 오류는 서버의 detail 문구를 그대로 던진다.
import type { Explanation, FileSlice, RepoSummary, SymbolSpan } from '../types'
import { parseSSE, type SSEMessage } from './sse'

export const API_URL: string = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8000'

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => null)
    throw new Error(body?.detail ?? `HTTP ${res.status}`)
  }
  return res.json() as Promise<T>
}

function post(path: string, body: unknown): Promise<Response> {
  return fetch(API_URL + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export async function indexRepo(url: string): Promise<RepoSummary> {
  return json(await post('/repos', { url }))
}

// 404 는 "없음"(null), 그 밖의 오류는 json() 처럼 throw
async function jsonOrNull<T>(res: Response): Promise<T | null> {
  if (res.status === 404) return null
  return json<T>(res)
}

// repoId 는 "owner/name@sha" — 경로에는 owner/name 만 쓴다.
function repoPath(repoId: string): string {
  const [owner, name] = repoId.split('@')[0].split('/')
  return `${API_URL}/repos/${owner}/${name}`
}

export async function readFile(repoId: string, path: string, start: number, end: number): Promise<FileSlice> {
  const q = new URLSearchParams({ path, start: String(start), end: String(end) })
  return json(await fetch(`${repoPath(repoId)}/files?${q}`))
}

// 인용 줄을 감싸는 심볼. 감싸는 심볼이 없으면 null.
export async function getSymbol(repoId: string, file: string, line: number): Promise<SymbolSpan | null> {
  const q = new URLSearchParams({ file, line: String(line) })
  return jsonOrNull(await fetch(`${repoPath(repoId)}/symbol?${q}`))
}

// 심볼 설명 (LLM 호출 — 비싸므로 드로어가 열릴 때 한 번만). 심볼이 없으면 null, LLM 오류(502)는 throw.
export async function explainSymbol(repoId: string, file: string, line: number): Promise<Explanation | null> {
  const q = new URLSearchParams({ file, line: String(line) })
  return jsonOrNull(await fetch(`${repoPath(repoId)}/explain?${q}`))
}

export async function sendFeedback(runId: string, score: 0 | 1, kind: 'thumbs' | 'citation_error') {
  return json(await post('/feedback', { run_id: runId, score, kind }))
}

// Supervisor 실행. SSE 메시지(update|result|error|done)마다 onEvent 를 부른다.
export async function runThread(
  threadId: string,
  repoUrl: string,
  message: string,
  onEvent: (m: SSEMessage) => void,
) {
  const res = await post(`/threads/${threadId}/runs`, { repo_url: repoUrl, message })
  if (!res.ok) await json(res) // 4xx·5xx → detail 로 throw
  if (!res.body) throw new Error('응답 본문이 없음')
  for await (const m of parseSSE(res.body)) onEvent(m)
}
