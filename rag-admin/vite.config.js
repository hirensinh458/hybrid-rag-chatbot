import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      // Proxy all /admin/* calls directly to FastAPI backend
      '/admin': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      // Proxy /pdfs/* for PDF preview
      '/pdfs': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/api': {                          // keep if other /api/* routes exist
        target: 'http://localhost:8000',
        changeOrigin: true,
        secure: false,
      },
      '/sync': {                         // ← add this
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})