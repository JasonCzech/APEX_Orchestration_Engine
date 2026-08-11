import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  timeout: 45_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: 'http://127.0.0.1:4174',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'node e2e/demo-server.mjs',
    url: 'http://127.0.0.1:4174/config.json',
    reuseExistingServer: false,
  },
})
