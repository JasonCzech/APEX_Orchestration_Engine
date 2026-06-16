import '@testing-library/jest-dom/vitest'

import { transferableAbortController } from 'node:util'

import { configure } from '@testing-library/react'
import { afterAll, afterEach, beforeAll } from 'vitest'

import { resetApexClient } from '@/api/apexClient'

import { server } from './server'

// RTL's waitFor/findBy* default to a 1s poll window — far too tight for
// navigations onto lazy() route modules: React Router only commits
// router.state.location after the route chunk's dynamic import resolves, and
// the heavy chunks (runs -> CodeMirror, analytics -> recharts) can take >1s to
// import under parallel-worker CPU contention. Every observed suite flake
// (analytics cards findBy, home/approvals/golden-configs navigation waitFors)
// was this 1s default expiring mid-import — vitest's 15s testTimeout never
// engaged because RTL gave up first. 10s keeps waitFor failures (with their
// DOM dumps) arriving before the 15s hard test timeout. Assertions and their
// success conditions are unchanged; passing tests resolve as soon as the
// condition holds.
configure({ asyncUtilTimeout: 10_000 })

// jsdom ships its own AbortController/AbortSignal, but the fetch stack in the
// test process is Node's undici, whose Request brand-checks signals against
// Node's AbortSignal. React Router's data router passes an AbortSignal into
// new Request(...) on every navigation, so swap the globals to Node's classes.
const nodeAbortController = transferableAbortController()
globalThis.AbortController = nodeAbortController.constructor as typeof AbortController
globalThis.AbortSignal = nodeAbortController.signal.constructor as typeof AbortSignal

function emptyClientRects(): DOMRectList {
  return {
    length: 0,
    item: () => null,
    [Symbol.iterator]: function* () {},
  } as unknown as DOMRectList
}

if (typeof Range !== 'undefined') {
  if (!Range.prototype.getClientRects) {
    Range.prototype.getClientRects = emptyClientRects
  }
  if (!Range.prototype.getBoundingClientRect) {
    Range.prototype.getBoundingClientRect = () => new DOMRect()
  }
}

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' })
})

afterEach(() => {
  server.resetHandlers()
  window.localStorage.clear()
  // The streaming resume store (src/streaming/resumeStore.ts) persists
  // lastEventId per run in sessionStorage; clear it so a test that streams
  // can't seed a Last-Event-ID resume for a later test in the same file.
  window.sessionStorage.clear()
  resetApexClient()
  document.documentElement.removeAttribute('data-theme')
})

afterAll(() => {
  server.close()
})
