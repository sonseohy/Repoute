// API 응답 타입 — 파이썬 스키마(pydantic 모델) 필드 이름과 같게 둔다.

export interface CodeRef {
  file: string
  line: number
  symbol: string | null
}

// reading_guide.py
export interface ReadingStep {
  order: number
  file: string
  reason: string
  ref: CodeRef
}

export interface ReadingGuide {
  overview: string
  entrypoints: CodeRef[]
  reading_order: ReadingStep[]
}

// walkthrough.py
export interface Step {
  order: number
  title: string
  explanation: string
  refs: CodeRef[]
}

export interface WalkthroughAnswer {
  question: string
  steps: Step[]
  summary: string
}

// supervisor.py — "다음 파일" 질문의 답 (읽기 순서에서 한 칸 앞으로)
export interface NextStep {
  order: number
  file: string
  reason: string
  ref: CodeRef
  remaining: number // 이 단계 뒤에 남은 단계 수
  hint: string | null
}

export interface Intent {
  kind: 'map' | 'ask' | 'next' | 'chat'
  reason: string
}

export type SupervisorEvent =
  | { type: 'IndexReady'; repo_id: string; cached: boolean; files: number }
  | { type: 'IntentDecided'; kind: Intent['kind']; reason: string }
  | { type: 'GateApplied'; from: Intent['kind']; to: Intent['kind']; reason: string } // 예: ask→map, map→next
  | { type: 'GuideReady'; steps: number; errors: number }
  | { type: 'AnswerReady'; steps: number; errors: number }
  | { type: 'NextStep'; order: number; remaining: number }
  | { type: 'Replied' }

// api/app.py — SSE 이벤트 data
export interface UpdateData {
  node: string
  events: SupervisorEvent[]
}

export interface RunResult {
  intent: Intent | null
  guide: ReadingGuide | null
  answer: WalkthroughAnswer | null
  next?: NextStep | null // 백엔드가 아직 안 보낼 수 있다
  reply: string | null
  validation_errors: string[]
  thread_id: string
  run_id: string | null
}

// POST /repos
export interface RepoSummary {
  repo_id: string // owner/name@abc1234
  files: number
  cached: boolean
  symbols: number
  calls: number
  core_modules: { module: string; score: number; refs: number }[]
}

// GET /repos/{owner}/{name}/files
export interface FileSlice {
  path: string
  start: number
  end: number
  lines: { n: number; text: string }[]
}

// GET /repos/{owner}/{name}/symbol — 인용 줄을 감싸는 함수·클래스의 범위
export interface SymbolSpan {
  name: string
  kind: string // function | class | method …
  file: string
  line_start: number
  line_end: number
  docstring: string | null
}

// GET /repos/{owner}/{name}/explain — 심볼 한 개의 짧은 설명 (fast 모델, 서버 캐시)
export interface Explanation {
  name: string
  kind: string
  line_start: number
  line_end: number
  what: string // 무엇을 하나 (1~2문장)
  watch: string[] // 꼭 봐야 할 코드 — 항목 안의 "(줄 N)" / "(줄 N~M)" 은 줄 링크가 된다
  docstring_ko: string // 원문 docstring 의 핵심 한국어 1~2문장 (docstring 이 없으면 빈 문자열)
  cached: boolean
}

// 화면의 대화 한 줄
export type Message =
  | { role: 'user'; id: string; text: string }
  | {
      role: 'agent'
      id: string
      lines: string[] // 진행 이벤트를 한국어로 바꾼 줄들
      pending: boolean
      result?: RunResult
      error?: string
    }
