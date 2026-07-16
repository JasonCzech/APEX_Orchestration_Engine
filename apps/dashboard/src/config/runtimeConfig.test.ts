import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  DEFAULT_RUNTIME_CONFIG,
  getRuntimeConfig,
  loadRuntimeConfig,
  MAX_RUNTIME_CONFIG_CHUNKS,
  RUNTIME_CONFIG_DEADLINE_MS,
  setRuntimeConfig,
} from './runtimeConfig'

afterEach(() => {
  setRuntimeConfig(DEFAULT_RUNTIME_CONFIG)
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('runtime configuration boundary', () => {
  it('canonicalizes complete HTTP origins before API clients consume them', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            apexOrigin: 'https://API.example.test:443/',
            langgraphOrigin: 'http://langgraph.example.test:2024',
          }),
          { status: 200, headers: { 'content-type': 'Application/JSON ; Charset=UTF-8' } },
        ),
      ),
    )

    await expect(loadRuntimeConfig()).resolves.toEqual({
      apexOrigin: 'https://api.example.test',
      langgraphOrigin: 'http://langgraph.example.test:2024',
    })
    expect(getRuntimeConfig()).toEqual({
      apexOrigin: 'https://api.example.test',
      langgraphOrigin: 'http://langgraph.example.test:2024',
    })
  })

  it('accepts and canonicalizes URL-parsed bracketed IPv6 origins', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            apexOrigin: 'http://[0:0:0:0:0:0:0:1]:8080',
            langgraphOrigin: 'https://[2001:0DB8:0:0:0:0:0:1]',
          }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        ),
      ),
    )

    await expect(loadRuntimeConfig()).resolves.toEqual({
      apexOrigin: 'http://[::1]:8080',
      langgraphOrigin: 'https://[2001:db8::1]',
    })
  })

  it('rejects a scoped IPv6 zone identifier', () => {
    expect(() =>
      setRuntimeConfig({ apexOrigin: 'http://[fe80::1%25en0]', langgraphOrigin: '' }),
    ).toThrow('Dashboard runtime configuration is invalid.')
  })

  it.each(['application/jsonp', 'text/application/json-malformed'])(
    'does not accept the malformed JSON media type %s',
    async (contentType) => {
      setRuntimeConfig({
        apexOrigin: 'https://old-api.example.test',
        langgraphOrigin: 'https://old-graph.example.test',
      })
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue(
          new Response(
            JSON.stringify({
              apexOrigin: 'https://untrusted-api.example.test',
              langgraphOrigin: 'https://untrusted-graph.example.test',
            }),
            { status: 200, headers: { 'content-type': contentType } },
          ),
        ),
      )

      await expect(loadRuntimeConfig()).resolves.toEqual(DEFAULT_RUNTIME_CONFIG)
      expect(getRuntimeConfig()).toEqual(DEFAULT_RUNTIME_CONFIG)
    },
  )

  it('fails closed on an explicit credential-bearing or path-bearing origin', async () => {
    const canary = 'runtime-origin-secret-canary'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            apexOrigin: `https://user:${canary}@api.example.test/v1`,
            langgraphOrigin: '',
          }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        ),
      ),
    )

    const error = await loadRuntimeConfig().catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(Error)
    expect((error as Error).message).toBe('Dashboard runtime configuration is invalid.')
    expect(String(error)).not.toContain(canary)
    expect(getRuntimeConfig()).toEqual(DEFAULT_RUNTIME_CONFIG)
  })

  it('rejects unknown credential-bearing fields without reflecting them', async () => {
    const canary = 'runtime-config-api-key-secret-canary'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ apexOrigin: '', langgraphOrigin: '', apiKey: canary }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        ),
      ),
    )

    const error = await loadRuntimeConfig().catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(Error)
    expect((error as Error).message).toBe('Dashboard runtime configuration is invalid.')
    expect(String(error)).not.toContain(canary)
    expect(getRuntimeConfig()).toEqual(DEFAULT_RUNTIME_CONFIG)
  })

  it('rejects a runtime config body that exceeds the deployment byte budget', async () => {
    const canary = 'oversized-runtime-config-secret-canary'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            apexOrigin: '',
            langgraphOrigin: '',
            padding: canary.repeat(1_024),
          }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        ),
      ),
    )

    const error = await loadRuntimeConfig().catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(Error)
    expect((error as Error).message).toBe('Dashboard runtime configuration is invalid.')
    expect(String(error)).not.toContain(canary)
    expect(getRuntimeConfig()).toEqual(DEFAULT_RUNTIME_CONFIG)
  })

  it('does not wait for an oversized stream cancellation to settle', async () => {
    let cancelCalled = false
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array(16 * 1024 + 1))
      },
      cancel() {
        cancelCalled = true
        return new Promise<void>(() => undefined)
      },
    })
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(body, {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
      ),
    )

    const outcome = await Promise.race([
      loadRuntimeConfig().catch((error: unknown) => error),
      new Promise<'stalled'>((resolve) => setTimeout(() => resolve('stalled'), 0)),
    ])

    expect(outcome).toBeInstanceOf(Error)
    expect((outcome as Error).message).toBe('Dashboard runtime configuration is invalid.')
    expect(cancelCalled).toBe(true)
  })

  it('bounds metadata growth from many tiny config chunks', async () => {
    let cancelCalled = false
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (let index = 0; index <= MAX_RUNTIME_CONFIG_CHUNKS; index += 1) {
          controller.enqueue(new Uint8Array([0x20]))
        }
      },
      cancel() {
        cancelCalled = true
        return new Promise<void>(() => undefined)
      },
    })
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(body, {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
      ),
    )

    await expect(loadRuntimeConfig()).rejects.toThrow(
      'Dashboard runtime configuration is invalid.',
    )
    expect(cancelCalled).toBe(true)
  })

  it('falls back after the bounded deadline when the initial fetch never settles', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => undefined)))

    const pending = loadRuntimeConfig()
    await vi.advanceTimersByTimeAsync(RUNTIME_CONFIG_DEADLINE_MS)

    await expect(pending).resolves.toEqual(DEFAULT_RUNTIME_CONFIG)
  })

  it('rejects redirects at fetch and falls back if a redirected response is synthesized', async () => {
    const response = new Response(
      JSON.stringify({
        apexOrigin: 'https://redirected-api.example.test',
        langgraphOrigin: 'https://redirected-graph.example.test',
      }),
      { headers: { 'content-type': 'application/json' } },
    )
    Object.defineProperty(response, 'redirected', { value: true })
    const fetchMock = vi.fn().mockResolvedValue(response)
    vi.stubGlobal('fetch', fetchMock)

    await expect(loadRuntimeConfig()).resolves.toEqual(DEFAULT_RUNTIME_CONFIG)
    expect(fetchMock).toHaveBeenCalledWith(
      '/config.json',
      expect.objectContaining({ cache: 'no-store', redirect: 'error' }),
    )
  })

  it('fails closed after the same deadline when an explicit body never yields', async () => {
    vi.useFakeTimers()
    let cancelCalled = false
    const body = new ReadableStream<Uint8Array>({
      pull() {
        return new Promise<void>(() => undefined)
      },
      cancel() {
        cancelCalled = true
        return new Promise<void>(() => undefined)
      },
    })
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(body, {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
      ),
    )

    const pending = loadRuntimeConfig()
    const rejected = expect(pending).rejects.toThrow(
      'Dashboard runtime configuration is invalid.',
    )
    await vi.advanceTimersByTimeAsync(RUNTIME_CONFIG_DEADLINE_MS)

    await rejected
    expect(cancelCalled).toBe(true)
  })

  it('rejects an oversized declared content length before reading the body', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response('{}', {
          status: 200,
          headers: {
            'content-type': 'application/json',
            'content-length': String(16 * 1024 + 1),
          },
        }),
      ),
    )

    await expect(loadRuntimeConfig()).rejects.toThrow(
      'Dashboard runtime configuration is invalid.',
    )
  })

  it('keeps the documented same-origin fallback for an absent config response', async () => {
    setRuntimeConfig({
      apexOrigin: 'https://old-api.example.test',
      langgraphOrigin: 'https://old-graph.example.test',
    })
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')))

    await expect(loadRuntimeConfig()).resolves.toEqual(DEFAULT_RUNTIME_CONFIG)
    expect(getRuntimeConfig()).toEqual(DEFAULT_RUNTIME_CONFIG)
  })
})
