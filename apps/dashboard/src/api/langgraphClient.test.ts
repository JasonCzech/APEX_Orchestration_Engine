import { beforeEach, describe, expect, it, vi } from 'vitest'

import { setApiKey } from '@/auth/keyStorage'

const { clientConstructor } = vi.hoisted(() => ({
  clientConstructor: vi.fn(),
}))

vi.mock('@langchain/langgraph-sdk', () => ({
  Client: clientConstructor,
}))

import { getLangGraphClient, resetLangGraphClient } from './langgraphClient'

describe('LangGraph client singleton', () => {
  beforeEach(() => {
    resetLangGraphClient()
    clientConstructor.mockReset()
  })

  it('retries construction after the cached promise rejects', async () => {
    const recoveredClient = { assistants: {}, runs: {}, threads: {} }
    clientConstructor
      .mockImplementationOnce(() => {
        throw new Error('transient chunk failure')
      })
      .mockImplementationOnce(() => recoveredClient)

    await expect(getLangGraphClient()).rejects.toThrow('transient chunk failure')
    await expect(getLangGraphClient()).resolves.toBe(recoveredClient)
    expect(clientConstructor).toHaveBeenCalledTimes(2)
  })

  it('rejects redirects without dropping SDK request headers or signals', async () => {
    const recoveredClient = { assistants: {}, runs: {}, threads: {} }
    clientConstructor.mockReturnValue(recoveredClient)
    setApiKey('apex_redirect_test_key')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}'))

    try {
      await expect(getLangGraphClient()).resolves.toBe(recoveredClient)
      const options = clientConstructor.mock.calls[0]?.[0] as {
        callerOptions: { fetch: typeof fetch }
      }
      const controller = new AbortController()
      const headers = new Headers({ 'x-api-key': 'apex_redirect_test_key' })

      await options.callerOptions.fetch('https://graph.example.test/runs', {
        headers,
        method: 'POST',
        signal: controller.signal,
      })

      expect(fetchMock).toHaveBeenCalledWith(
        'https://graph.example.test/runs',
        expect.objectContaining({
          headers,
          method: 'POST',
          signal: controller.signal,
          redirect: 'error',
        }),
      )
    } finally {
      fetchMock.mockRestore()
    }
  })
})
