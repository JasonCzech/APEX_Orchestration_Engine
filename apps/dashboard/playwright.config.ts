import { defineConfig } from '@playwright/test'

const port = Number(process.env.APEX_DASHBOARD_TEST_PORT ?? 4174)

export default defineConfig({
  testDir: './e2e',
  timeout: 45_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'node e2e/demo-server.mjs',
    url: `http://127.0.0.1:${port}/config.json`,
    reuseExistingServer: false,
  },
})
