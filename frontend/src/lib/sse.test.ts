import { describe, expect, test } from 'vitest'
import { parseSSE, type SSEMessage } from './sse'

// 문자열 청크들을 그대로 흘리는 가짜 응답 본문
function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder()
  return new ReadableStream({
    start(c) {
      chunks.forEach((s) => c.enqueue(enc.encode(s)))
      c.close()
    },
  })
}

async function collect(chunks: string[]): Promise<SSEMessage[]> {
  const out: SSEMessage[] = []
  for await (const m of parseSSE(streamOf(chunks))) out.push(m)
  return out
}

describe('parseSSE', () => {
  test('한 청크에 이벤트 두 개가 오면 두 개로 나눈다', async () => {
    const chunk = 'event: update\ndata: {"node":"classify","events":[]}\n\nevent: done\ndata: {}\n\n'
    expect(await collect([chunk])).toEqual([
      { event: 'update', data: { node: 'classify', events: [] } },
      { event: 'done', data: {} },
    ])
  })

  test('이벤트 하나가 두 청크(한글 바이트 중간 포함)로 나뉘어도 하나로 합친다', async () => {
    const full = new TextEncoder().encode('event: error\ndata: {"message":"레포 없음"}\n\n')
    const cut = full.length - 12 // 한글 글자 중간에서 자른다
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(full.slice(0, cut))
        c.enqueue(full.slice(cut))
        c.close()
      },
    })
    const out: SSEMessage[] = []
    for await (const m of parseSSE(stream)) out.push(m)
    expect(out).toEqual([{ event: 'error', data: { message: '레포 없음' } }])
  })
})
