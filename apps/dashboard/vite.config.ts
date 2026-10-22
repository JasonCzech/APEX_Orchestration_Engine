import { fileURLToPath } from 'node:url'
import type { IncomingMessage } from 'node:http'

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import { configDefaults } from 'vitest/config'

const proxyTarget = process.env.APEX_API_PROXY ?? 'http://127.0.0.1:2024'

/**
 * Paths proxied to the backend (LangGraph server + mounted /v1 domain API) in dev.
 * `/runs` collides with the SPA route of the same name, so browser navigations
 * (Accept: text/html) bypass the proxy and receive index.html, while API/SSE
 * requests (application/json / text/event-stream) are forwarded.
 */
const API_PROXY_PATHS = ['/v1', '/threads', '/runs', '/assistants', '/ok', '/ready']

function bypassHtmlNavigation(req: IncomingMessage): string | undefined {
  return req.headers.accept?.includes('text/html') ? '/index.html' : undefined
}

export default defineConfig(() => {
  return {
    plugins: [react()],
    resolve: {
      alias: [
        { find: '@/dev-data', replacement: fileURLToPath(new URL('./src/dev-data/index.ts', import.meta.url)) },
        { find: '@', replacement: fileURLToPath(new URL('./src', import.meta.url)) },
      ],
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
            const normalizedId = id.replaceAll('\\', '/')
            if (!normalizedId.includes('node_modules')) return undefined
            if (normalizedId.includes('/@langchain/')) return 'vendor-langgraph'
            // Keep report parsing out of the dashboard's initial shared bundle.
            if (/\/(?:react-markdown|remark-[^/]+|micromark[^/]*|mdast-[^/]+|hast-[^/]+|unist-[^/]+|unified|vfile[^/]*)\//.test(normalizedId))
              return 'vendor-markdown'
            if (
              normalizedId.includes('/@codemirror/') ||
              normalizedId.includes('/@uiw/') ||
              normalizedId.includes('/@lezer/')
            )
              return 'vendor-codemirror'
            // recharts + its d3/victory-vendor dependency tree (D2 engine strip).
            if (
              normalizedId.includes('/recharts/') ||
              normalizedId.includes('/recharts-scale/') ||
              normalizedId.includes('/victory-vendor/') ||
              normalizedId.includes('/d3-') ||
              normalizedId.includes('/internmap/') ||
              normalizedId.includes('/delaunator/') ||
              normalizedId.includes('/robust-predicates/')
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
      exclude: [...configDefaults.exclude, 'e2e/**'],
      css: false,
      // Layered timeouts (D8 flake-hunt): RTL's waitFor/findBy* poll window is
      // raised to 10s in src/test/setup.ts (configure({ asyncUtilTimeout })) —
      // lazy() route chunks (CodeMirror, recharts) can take >1s to dynamic-import
      // under parallel workers, and RTL's 1s default expiring mid-import was the
      // actual flake source. The 15s vitest timeout sits ABOVE that window so
      // genuine failures surface as waitFor errors (with DOM dumps) rather than
      // opaque hard test timeouts.
      testTimeout: 15_000,
      hookTimeout: 15_000,
      coverage: {
        provider: 'v8',
        reporter: ['text', 'lcov'],
        // Keep uncovered source files in the denominator on Vitest 4.
        include: ['src/**/*.{ts,tsx}'],
        exclude: ['src/**/*.d.ts', 'src/**/__tests__/**', 'src/test/**'],
        thresholds: {
          branches: 70,
          functions: 70,
          lines: 70,
          statements: 70,
        },
      },
    },
  }
})
