import { expect, test } from 'vitest'
import { parseLineRefs } from './lineRefs'

test('parseLineRefs 는 "(줄 N)" 과 "(줄 N~M)" 을 줄 링크 조각으로, 나머지는 글자 그대로 나눈다', () => {
  expect(parseLineRefs('None 이면 기본값 (줄 12), 예외는 (줄 20~24) 에서')).toEqual([
    'None 이면 기본값 ',
    { text: '(줄 12)', start: 12, end: 12 },
    ', 예외는 ',
    { text: '(줄 20~24)', start: 20, end: 24 },
    ' 에서',
  ])
  expect(parseLineRefs('줄 표기 없음')).toEqual(['줄 표기 없음'])
  expect(parseLineRefs('(줄 3)')).toEqual([{ text: '(줄 3)', start: 3, end: 3 }])
})
