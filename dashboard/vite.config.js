import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist', // Assurez-vous que c'est bien 'dist'
  },
  server: {
    // En développement, /api/* est transféré vers l'API FastAPI locale,
    // ce qui rend fetch("/api/ai") homogène entre dev et production.
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})

