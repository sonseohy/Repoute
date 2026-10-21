// Supervisor 진행 이벤트 → 화면에 옅게 보일 한국어 한 줄.
import type { SupervisorEvent } from '../types'

const failed = (n: number) => (n ? `, 검증 실패 ${n}건` : '')

// 의도 kind → 화면 이름. 표에 없는 kind(map·ask·chat 등)는 영문 그대로 보인다.
const KIND_LABEL: Record<string, string> = { next: '다음 단계' }
const kindLabel = (kind: string) => KIND_LABEL[kind] ?? kind

export function describeEvent(e: SupervisorEvent): string {
  switch (e.type) {
    case 'IndexReady':
      return `인덱스 준비 · ${e.cached ? 'cache hit' : '새로 인덱싱'} · 파일 ${e.files}개`
    case 'IntentDecided':
      return `의도: ${kindLabel(e.kind)} — ${e.reason}`
    case 'GateApplied':
      return `게이트: ${kindLabel(e.from)} → ${kindLabel(e.to)}` // 예: map → 다음 단계
    case 'GuideReady':
      return `읽기 지도 생성 완료 (${e.steps}단계${failed(e.errors)})`
    case 'AnswerReady':
      return `따라가기 답 완성 (${e.steps}단계${failed(e.errors)})`
    case 'NextStep':
      return `다음 단계 ${e.order} (남은 ${e.remaining}개)`
    case 'Replied':
      return '안내 답변'
    default:
      return String((e as { type?: string }).type ?? '알 수 없는 이벤트')
  }
}
