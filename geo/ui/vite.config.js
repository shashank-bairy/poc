import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  server: {
    port: 5173,
    // The API runs separately (uvicorn on 8000); proxying keeps the frontend
    // origin-clean so no CORS handling is needed in the browser.
    proxy: { '/api': 'http://localhost:8000' },
  },
  plugins: [react()],
})
