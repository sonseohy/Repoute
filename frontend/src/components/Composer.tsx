// 하단 입력창: Enter 전송, Shift+Enter 줄바꿈, 높이 자동.
import { useRef, useState, type KeyboardEvent } from 'react'

interface Props {
  disabled: boolean
  hint: string
  onSend: (text: string) => void
}

export function Composer({ disabled, hint, onSend }: Props) {
  const [text, setText] = useState('')
  const area = useRef<HTMLTextAreaElement>(null)

  function resize() {
    const el = area.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`
  }

  function send() {
    if (disabled || !text.trim()) return
    onSend(text.trim())
    setText('')
    requestAnimationFrame(resize)
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // 한글 조합 중 Enter 는 전송하지 않는다 (마지막 글자 중복 방지)
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      send()
    }
  }

  return (
    <div className="composer-wrap">
      <div className="composer">
        <textarea
          ref={area}
          rows={1}
          value={text}
          disabled={disabled}
          placeholder={hint}
          aria-label="메시지 입력"
          onChange={(e) => {
            setText(e.target.value)
            resize()
          }}
          onKeyDown={onKeyDown}
        />
        <button className="btn btn-primary" type="button" onClick={send} disabled={disabled || !text.trim()} aria-label="보내기">
          보내기
        </button>
      </div>
    </div>
  )
}
