import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const backendTarget = process.env.VITE_BACKEND_TARGET || 'http://localhost:8787'
/** finus_nat ``nat serve`` (default ``run.sh`` chat: FINUS_CHAT_PORT=8765). */
const natTarget = process.env.VITE_NAT_TARGET || 'http://127.0.0.1:8765'

const finUsProxy = {
  '/api': {
    target: backendTarget,
    changeOrigin: true,
    ws: true,
  },
  '/health': {
    target: backendTarget,
    changeOrigin: true,
  },
  '/nat-agent': {
    target: natTarget,
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/nat-agent/, '') || '/',
  },
} as const

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { ...finUsProxy },
  },
  /** ``npm run preview``에서도 NAT/백엔드 프록시를 쓰려면 동일 설정 필요 */
  preview: {
    proxy: { ...finUsProxy },
  },
})
