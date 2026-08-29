import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: process.env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:8016',
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
      '/coordinator-api': {
        target: process.env.VITE_COORDINATOR_API_PROXY_TARGET ?? 'http://127.0.0.1:8020',
        rewrite: (path) => path.replace(/^\/coordinator-api/, ''),
      },
      '/management-api': {
        target: process.env.VITE_MANAGEMENT_API_PROXY_TARGET ?? 'http://127.0.0.1:8014',
        rewrite: (path) => path.replace(/^\/management-api/, ''),
      },
      '/terminal-host': {
        target: process.env.VITE_TERMINAL_HOST_PROXY_TARGET ?? 'http://127.0.0.1:8022',
        rewrite: (path) => path.replace(/^\/terminal-host/, ''),
        ws: true,
      },
    },
  },
})
