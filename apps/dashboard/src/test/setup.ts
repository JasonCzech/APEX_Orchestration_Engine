import '@testing-library/jest-dom/vitest'

import { transferableAbortController } from 'node:util'

import { afterAll, afterEach, beforeAll } from 'vitest'

import { resetApexClient } from '@/api/apexClient'

import { server } from './server'

// jsdom ships its own AbortController/AbortSignal, but the fetch stack in the
// test process is Node's undici, whose Request brand-checks signals against
// Node's AbortSignal. React Router's data router passes an AbortSignal into
// new Request(...) on every navigation, so swap the globals to Node's classes.
const nodeAbortController = transferableAbortController()
globalThis.AbortController = nodeAbortController.constructor as typeof AbortController
globalThis.AbortSignal = nodeAbortController.signal.constructor as typeof AbortSignal

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' })
})

afterEach(() => {
  server.resetHandlers()
  window.localStorage.clear()
  resetApexClient()
  document.documentElement.removeAttribute('data-theme')
})

afterAll(() => {
  server.close()
})
