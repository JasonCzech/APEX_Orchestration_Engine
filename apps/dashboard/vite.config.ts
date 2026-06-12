/// <reference types="vitest/config" />
import { fileURLToPath } from 'node:url'
import type { IncomingMessage } from 'node:http'

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const proxyTarget = process.env.APEX_API_PROXY ?? 'http://127.0.0.1:2024'

/**
 * Paths proxied to the backend (LangGraph server + mounted /v1 domain API) in dev.
 * `/runs` collides with the SPA route of the same name, so browser navigations
 * (Accept: text/html) bypass the proxy and receive index.html, while API/SSE
 * requests (application/json / text/event-stream) are forwarded.
 */
const API_PROXY_PATHS = ['/v1', '/threads', '/runs', '/assistants', '/ok']

function bypassHtmlNavigation(req: IncomingMessage): string | undefined {
  return req.headers.accept?.includes('text/html') ? '/index.html' : undefined
}

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 3000,
    proxy: Object.fromEntries(
      API_PROXY_PATHS.map((path) => [
        path,
        { target: proxyTarget, changeOrigin: true, bypass: bypassHtmlNavigation },
      ]),
    ),
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return undefined
          if (id.includes('/@langchain/')) return 'vendor-langgraph'
          if (id.includes('/@codemirror/') || id.includes('/@uiw/') || id.includes('/@lezer/'))
            return 'vendor-codemirror'
          // recharts + its d3/victory-vendor dependency tree (D2 engine strip).
          if (
            id.includes('/recharts/') ||
            id.includes('/recharts-scale/') ||
            id.includes('/victory-vendor/') ||
            id.includes('/d3-') ||
            id.includes('/internmap/') ||
            id.includes('/delaunator/') ||
            id.includes('/robust-predicates/')
          )
            return 'vendor-recharts'
          return 'vendor'
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    // Generous timeouts: CodeMirror-rendering and keyboard-driven tests are
    // timing-sensitive and flake under parallel workers on a loaded machine.
    testTimeout: 15_000,
    hookTimeout: 15_000,
  },
})
