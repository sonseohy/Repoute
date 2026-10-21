import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// https://vite.dev/config/ — test 는 vitest 설정 (기본 node, 컴포넌트 테스트만 파일 상단에서 jsdom)
export default defineConfig({
  plugins: [react()],
  test: { environment: 'node' },
})
