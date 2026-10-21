// "꼭 봐야 할 코드" 항목 안의 "(줄 N)" · "(줄 N~M)" 을 찾아 줄 링크 조각으로 나눈다.
export type LineRefPart = string | { text: string; start: number; end: number }

const LINE_REF = /\(줄 (\d+)(?:~(\d+))?\)/g

export function parseLineRefs(text: string): LineRefPart[] {
  const parts: LineRefPart[] = []
  let last = 0
  for (const m of text.matchAll(LINE_REF)) {
    const at = m.index ?? 0
    if (at > last) parts.push(text.slice(last, at))
    const start = Number(m[1])
    parts.push({ text: m[0], start, end: m[2] ? Number(m[2]) : start })
    last = at + m[0].length
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts
}
