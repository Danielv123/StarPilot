import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    sourcemap: true,
    emptyOutDir: true,
  },
  server: {
    host: '127.0.0.1',
    port: 4173,
    proxy: {
      '/api': {
        target: process.env.COMMA_COMPANION_API ?? 'http://127.0.0.1:8080',
        changeOrigin: true,
        ws: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    css: true,
  },
})
