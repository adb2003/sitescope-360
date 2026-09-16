import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'node:path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  server: {
    port: 5173,
    // Proxy /api during development so the browser sees one origin. That matters for more than
    // CORS: signed asset URLs the API mints point at PUBLIC_BASE_URL, and keeping the dev origin
    // aligned with production behaviour means the viewer loads textures the same way in both.
    proxy: {
      '/api': {
        target: process.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000',
        changeOrigin: true,
        // The event socket goes through the same prefix. Without this the proxy answers the
        // upgrade request with a 404 and the push feature is missing in development only.
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    rollupOptions: {
      output: {
        // three.js is ~600kB; splitting it keeps the dashboard's first paint off the critical
        // path of the 360 viewer, which most sessions never open.
        manualChunks: {
          three: ['three'],
          vendor: ['react', 'react-dom', 'react-router-dom', '@tanstack/react-query'],
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    globals: true,
    css: false,
  },
})
