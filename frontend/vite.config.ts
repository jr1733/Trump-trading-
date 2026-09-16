import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The API runs on 8000 in dev; proxying keeps the origin identical so the
    // service worker and the API share one origin, as they do in production.
    proxy: {
      '/api': { target: process.env.VITE_API_TARGET ?? 'http://localhost:8000', changeOrigin: true },
    },
  },
  build: { outDir: 'dist', sourcemap: false },
})
