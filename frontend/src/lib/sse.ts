// POST 응답의 SSE 를 읽는다 (EventSource 는 GET 만 되므로 fetch 스트림을 직접 파싱).

export interface SSEMessage {
  event: string
  data: unknown
}

// "event: x\ndata: {...}" 블록 하나를 해석한다. 비어 있으면 null.
function parseBlock(block: string): SSEMessage | null {
  let event = 'message'
  const data: string[] = []
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
  }
  if (!data.length && event === 'message') return null
  return { event, data: data.length ? JSON.parse(data.join('\n')) : {} }
}

// 청크 경계와 상관없이 빈 줄(\n\n)로 끝난 블록만 내보낸다.
export async function* parseSSE(stream: ReadableStream<Uint8Array>): AsyncGenerator<SSEMessage> {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
    let i: number
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const msg = parseBlock(buf.slice(0, i))
      buf = buf.slice(i + 2)
      if (msg) yield msg
    }
  }
}
