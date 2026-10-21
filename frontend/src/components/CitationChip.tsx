// `file:line symbol` 인용 칩. 검증 실패 인용은 색을 바꾸고 title 에 오류를 보인다.
import type { CodeRef } from '../types'

interface Props {
  codeRef: CodeRef
  error?: string
  onOpen: (ref: CodeRef) => void
}

export function CitationChip({ codeRef, error, onOpen }: Props) {
  const label = `${codeRef.file}:${codeRef.line}`
  return (
    <button
      type="button"
      className={error ? 'chip chip-bad' : 'chip'}
      title={error ?? '코드 보기'}
      aria-label={`${label} 코드 보기${error ? ' (검증 실패)' : ''}`}
      onClick={() => onOpen(codeRef)}
    >
      <span className="chip-loc">{label}</span>
      {codeRef.symbol && <span className="chip-sym">{codeRef.symbol}</span>}
    </button>
  )
}
