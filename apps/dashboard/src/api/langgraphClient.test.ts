import { beforeEach, describe, expect, it, vi } from 'vitest'

import { bumpSessionRevision, setApiKey } from '@/auth/keyStorage'

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

  it('does not clone a consumed POST request after the server responds', async () => {
    const recoveredClient = { assistants: {}, runs: {}, threads: {} }
    clientConstructor.mockReturnValue(recoveredClient)
    setApiKey('apex_post_body_key')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(
      async (input) => {
        expect(input).toBeInstanceOf(Request)
        expect(await (input as Request).text()).toBe('{"config":{"engine":"sim"}}')
        return new Response('{}')
      },
    )

    try {
      await expect(getLangGraphClient()).resolves.toBe(recoveredClient)
      const options = clientConstructor.mock.calls[0]?.[0] as {
        callerOptions: { fetch: typeof fetch }
      }
      const request = new Request('https://graph.example.test/assistants/config', {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'x-api-key': 'apex_post_body_key',
        },
        body: '{"config":{"engine":"sim"}}',
      })

      await expect(options.callerOptions.fetch(request)).resolves.toBeInstanceOf(
        Response,
      )
      expect(request.bodyUsed).toBe(true)
    } finally {
      fetchMock.mockRestore()
    }
  })

  it('rebuilds the singleton after a same-key semantic session change', async () => {
    const firstClient = { assistants: {}, runs: {}, threads: {} }
    const secondClient = { assistants: {}, runs: {}, threads: {} }
    clientConstructor.mockReturnValueOnce(firstClient).mockReturnValueOnce(secondClient)
    setApiKey('apex_same_key_session')

    await expect(getLangGraphClient()).resolves.toBe(firstClient)
    bumpSessionRevision()
    await expect(getLangGraphClient()).resolves.toBe(secondClient)

    expect(clientConstructor).toHaveBeenCalledTimes(2)
  })

  it('rejects an SDK response that belongs to a superseded semantic session', async () => {
    const recoveredClient = { assistants: {}, runs: {}, threads: {} }
    clientConstructor.mockReturnValue(recoveredClient)
    setApiKey('apex_inflight_session')
    let resolveFetch: ((response: Response) => void) | undefined
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve
        }),
    )

    try {
      await expect(getLangGraphClient()).resolves.toBe(recoveredClient)
      const options = clientConstructor.mock.calls[0]?.[0] as {
        callerOptions: { fetch: typeof fetch }
      }
      const request = options.callerOptions.fetch('https://graph.example.test/runs', {
        headers: { 'x-api-key': 'apex_inflight_session' },
      })

      bumpSessionRevision()
      resolveFetch?.(new Response('{}'))

      await expect(request).rejects.toThrow(
        'Authentication changed while the request was in flight.',
      )
    } finally {
      fetchMock.mockRestore()
    }
  })
})
