import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const managementApiProxy = {
  target: process.env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:8014',
  rewrite: (path: string) => path.replace(/^\/api/, ''),
}

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5174,
    strictPort: true,
    proxy: { '/api': managementApiProxy },
  },
  preview: {
    host: '127.0.0.1',
    port: 5174,
    strictPort: true,
    proxy: { '/api': managementApiProxy },
  },
})
