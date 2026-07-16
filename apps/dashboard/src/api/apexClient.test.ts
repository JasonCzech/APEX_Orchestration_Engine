import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'

import { setApiKey } from '@/auth/keyStorage'
import { server } from '@/test/server'

import { getApexClient, onUnauthorized } from './apexClient'

describe('Apex client authentication middleware', () => {
  it('ignores a 401 response sent with a superseded credential generation', async () => {
    let requestStarted!: () => void
    let releaseResponse!: () => void
    const started = new Promise<void>((resolve) => {
      requestStarted = resolve
    })
    const release = new Promise<void>((resolve) => {
      releaseResponse = resolve
    })
    server.use(
      http.get('*/v1/system/info', async () => {
        requestStarted()
        await release
        return HttpResponse.json({ detail: 'expired key' }, { status: 401 })
      }),
    )
    setApiKey('apex_old_key')
    const unauthorized = vi.fn()
    const unsubscribe = onUnauthorized(unauthorized)

    try {
      const response = getApexClient().GET('/v1/system/info')
      await started
      setApiKey('apex_new_key')
      releaseResponse()
      await response

      expect(unauthorized).not.toHaveBeenCalled()
    } finally {
      unsubscribe()
    }
  })

  it('reports a 401 for the current credential generation', async () => {
    const redirectModes: RequestRedirect[] = []
    server.use(
      http.get('*/v1/system/info', ({ request }) => {
        redirectModes.push(request.redirect)
        return HttpResponse.json({ detail: 'expired key' }, { status: 401 })
      }),
    )
    setApiKey('apex_current_key')
    const unauthorized = vi.fn()
    const unsubscribe = onUnauthorized(unauthorized)

    try {
      await getApexClient().GET('/v1/system/info')
      expect(unauthorized).toHaveBeenCalledOnce()
      expect(redirectModes).toEqual(['error'])
    } finally {
      unsubscribe()
    }
  })
})
