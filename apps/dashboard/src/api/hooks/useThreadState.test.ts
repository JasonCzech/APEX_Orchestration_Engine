import { describe, expect, it } from 'vitest'

import {
  TERMINAL_THREAD_STATE_REFETCH_MS,
  THREAD_STATE_REFETCH_MS,
  threadStateRefetchInterval,
} from './useThreadState'

describe('threadStateRefetchInterval', () => {
  it.each(['busy', 'interrupted'])('polls active status %s at the fast cadence', (status) => {
    expect(threadStateRefetchInterval(status)).toBe(THREAD_STATE_REFETCH_MS)
  })

  it.each(['idle', 'error', null, undefined])(
    'keeps polling terminal or unknown status %s at the liveness cadence',
    (status) => {
      expect(threadStateRefetchInterval(status)).toBe(TERMINAL_THREAD_STATE_REFETCH_MS)
    },
  )
})
