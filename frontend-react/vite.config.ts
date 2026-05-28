import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const backendTarget = process.env.VITE_BACKEND_TARGET || 'http://localhost:8000'
const natTarget = process.env.VITE_NAT_TARGET || 'http://localhost:8001'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: backendTarget,
        changeOrigin: true,
        ws: true,
      },
      '/health': {
        target: backendTarget,
        changeOrigin: true,
      },
      '/nat': {
        target: natTarget,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/nat/, ''),
      },
    }
  }
})
