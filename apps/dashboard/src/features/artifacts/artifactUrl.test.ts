import { afterEach, describe, expect, it, vi } from 'vitest'

import { onUnauthorized } from '@/api/apexClient'
import { bumpSessionRevision, setApiKey } from '@/auth/keyStorage'

import {
  ARTIFACT_READ_ERROR,
  ARTIFACT_READ_IDLE_MS,
  artifactKeyFromUri,
  artifactProxyUrl,
  fetchArtifactBytes,
  readBoundedArtifactBody,
} from './artifactUrl'

afterEach(() => vi.useRealTimers())

describe('artifactKeyFromUri', () => {
  it('treats everything after memory:// as the store key (no host segment)', () => {
    expect(artifactKeyFromUri('memory://transcripts/thread-1/execution/attempt-1')).toBe(
      'transcripts/thread-1/execution/attempt-1',
    )
    expect(artifactKeyFromUri('memory://single-key')).toBe('single-key')
  })

  it('strips the bucket from s3:// uris and keeps the rest as the key', () => {
    expect(artifactKeyFromUri('s3://apex-artifacts/reports/thread-1/load-report.json')).toBe(
      'reports/thread-1/load-report.json',
    )
  })

  it('decodes canonical durable artifact keys exactly once', () => {
    expect(
      artifactKeyFromUri(
        'apex-artifact:///transcripts/thread-1/story%20analysis/attempt-1.txt',
      ),
    ).toBe('transcripts/thread-1/story analysis/attempt-1.txt')
    expect(artifactKeyFromUri('apex-artifact:///reports/literal%252Fsegment.json')).toBe(
      'reports/literal%2Fsegment.json',
    )
  })

  it('rejects malformed and unsupported uris', () => {
    expect(artifactKeyFromUri('memory://')).toBeNull()
    expect(artifactKeyFromUri('s3://bucket-only')).toBeNull()
    expect(artifactKeyFromUri('s3://bucket/')).toBeNull()
    expect(artifactKeyFromUri('apex-artifact:///')).toBeNull()
    expect(artifactKeyFromUri('apex-artifact:///bad%escape')).toBeNull()
    expect(artifactKeyFromUri('apex-artifact:///key?query')).toBeNull()
    expect(artifactKeyFromUri('apex-artifact:///key#fragment')).toBeNull()
    expect(artifactKeyFromUri('apex-artifact:///key%0Anewline')).toBeNull()
    expect(artifactKeyFromUri('file:///tmp/whatever')).toBeNull()
    expect(artifactKeyFromUri('https://example.com/x')).toBeNull()
    expect(artifactKeyFromUri(undefined)).toBeNull()
    expect(artifactKeyFromUri(null)).toBeNull()
    expect(artifactKeyFromUri('')).toBeNull()
  })
})

describe('artifactProxyUrl', () => {
  it('builds the same-origin /v1/artifacts proxy URL with literal slashes', () => {
    expect(artifactProxyUrl('memory://transcripts/thread-1/execution/attempt-1')).toBe(
      `${window.location.origin}/v1/artifacts/transcripts/thread-1/execution/attempt-1`,
    )
    expect(artifactProxyUrl('s3://apex-artifacts/reports/r-1.json')).toBe(
      `${window.location.origin}/v1/artifacts/reports/r-1.json`,
    )
    expect(
      artifactProxyUrl(
        'apex-artifact:///transcripts/thread-1/story%20analysis/attempt-1.txt',
      ),
    ).toBe(
      `${window.location.origin}/v1/artifacts/transcripts/thread-1/story%20analysis/attempt-1.txt`,
    )
  })

  it('percent-encodes within segments but never the separators', () => {
    expect(artifactProxyUrl('memory://reports/with space/file.json')).toBe(
      `${window.location.origin}/v1/artifacts/reports/with%20space/file.json`,
    )
    expect(artifactProxyUrl('apex-artifact:///reports/literal%252Fsegment.json')).toBe(
      `${window.location.origin}/v1/artifacts/reports/literal%252Fsegment.json`,
    )
  })

  it('returns null for non-proxyable uris', () => {
    expect(artifactProxyUrl('https://example.com/x')).toBeNull()
    expect(artifactProxyUrl(undefined)).toBeNull()
  })
})

describe('artifact response boundary', () => {
  it('preserves binary bytes while enforcing redirect and abort policy', async () => {
    setApiKey('apex_artifact_key')
    const response = new Response(new Uint8Array([0, 255, 1, 128]), {
      headers: { 'content-type': 'application/octet-stream' },
    })
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response)
    const controller = new AbortController()

    try {
      const artifact = await fetchArtifactBytes(
        `${window.location.origin}/v1/artifacts/binary`,
        controller.signal,
      )

      expect([...new Uint8Array(await artifact.blob.arrayBuffer())]).toEqual([0, 255, 1, 128])
      expect(artifact.mediaType).toBe('application/octet-stream')
      const init = fetchMock.mock.calls[0]?.[1]
      expect(init).toEqual(
        expect.objectContaining({ signal: expect.any(AbortSignal), redirect: 'error' }),
      )
      expect(new Headers(init?.headers).get('x-api-key')).toBe('apex_artifact_key')
    } finally {
      fetchMock.mockRestore()
    }
  })

  it('rejects an oversized declared length without waiting for cancellation', async () => {
    let cancelled = false
    const body = new ReadableStream<Uint8Array>({
      cancel() {
        cancelled = true
        return new Promise<void>(() => undefined)
      },
    })
    const response = new Response(body, {
      headers: { 'content-length': '5', 'content-type': 'application/octet-stream' },
    })

    const outcome = await Promise.race([
      readBoundedArtifactBody(response, 4).catch((error: unknown) => error),
      new Promise<'stalled'>((resolve) => setTimeout(() => resolve('stalled'), 0)),
    ])

    expect(outcome).toBeInstanceOf(Error)
    expect((outcome as Error).message).toBe(ARTIFACT_READ_ERROR)
    expect(cancelled).toBe(true)
  })

  it('rejects an oversized chunk when Content-Length is absent', async () => {
    let cancelled = false
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array([1, 2, 3, 4, 5]))
      },
      cancel() {
        cancelled = true
        return new Promise<void>(() => undefined)
      },
    })

    await expect(readBoundedArtifactBody(new Response(body), 4)).rejects.toThrow(
      ARTIFACT_READ_ERROR,
    )
    expect(cancelled).toBe(true)
  })

  it('bounds metadata growth from many tiny transport chunks', async () => {
    let cancelled = false
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (let index = 0; index < 5; index += 1) controller.enqueue(new Uint8Array([index]))
      },
      cancel() {
        cancelled = true
      },
    })

    await expect(readBoundedArtifactBody(new Response(body), 100, 4)).rejects.toThrow(
      ARTIFACT_READ_ERROR,
    )
    expect(cancelled).toBe(true)
  })

  it('aborts and rejects when an artifact body stops making progress', async () => {
    vi.useFakeTimers()
    let cancelled = false
    const body = new ReadableStream<Uint8Array>({
      cancel() {
        cancelled = true
        return new Promise<void>(() => undefined)
      },
    })
    const pending = readBoundedArtifactBody(new Response(body), 100)
    const rejected = expect(pending).rejects.toThrow(ARTIFACT_READ_ERROR)

    await vi.advanceTimersByTimeAsync(ARTIFACT_READ_IDLE_MS)

    await rejected
    expect(cancelled).toBe(true)
  })

  it('rejects promptly when the caller cancels a fetch whose transport ignores abort', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockImplementation(() => new Promise<Response>(() => undefined))
    const controller = new AbortController()
    try {
      const pending = fetchArtifactBytes('/v1/artifacts/stalled', controller.signal)
      const rejected = expect(pending).rejects.toThrow(ARTIFACT_READ_ERROR)
      controller.abort()
      await rejected
    } finally {
      fetchMock.mockRestore()
    }
  })

  it('notifies authentication when the current credential receives a 401', async () => {
    setApiKey('apex_expired_artifact_key')
    const unauthorized = vi.fn()
    const unsubscribe = onUnauthorized(unauthorized)
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(null, { status: 401 }))

    try {
      await expect(fetchArtifactBytes('/v1/artifacts/expired')).rejects.toThrow(
        'Artifact request failed (401)',
      )
      expect(unauthorized).toHaveBeenCalledTimes(1)
    } finally {
      unsubscribe()
      fetchMock.mockRestore()
    }
  })

  it('discards an artifact request when the API key changes in flight', async () => {
    setApiKey('apex_artifact_principal_a')
    let resolveFetch!: (response: Response) => void
    const transport = new Promise<Response>((resolve) => {
      resolveFetch = resolve
    })
    let cancelled = false
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(() => transport)

    try {
      const pending = fetchArtifactBytes('/v1/artifacts/principal-a')
      const rejected = expect(pending).rejects.toThrow(ARTIFACT_READ_ERROR)
      setApiKey('apex_artifact_principal_b')
      await rejected
      resolveFetch(
        new Response(
          new ReadableStream<Uint8Array>({
            cancel() {
              cancelled = true
            },
          }),
        ),
      )
      await transport
      await Promise.resolve()
      expect(cancelled).toBe(true)
    } finally {
      fetchMock.mockRestore()
    }
  })

  it('discards an artifact request when the semantic session changes in flight', async () => {
    setApiKey('apex_artifact_shared_key')
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockImplementation(() => new Promise<Response>(() => undefined))

    try {
      const pending = fetchArtifactBytes('/v1/artifacts/old-scope')
      const rejected = expect(pending).rejects.toThrow(ARTIFACT_READ_ERROR)
      bumpSessionRevision()
      await rejected
    } finally {
      fetchMock.mockRestore()
    }
  })

  it.each([
    'https://attacker.example/v1/artifacts/report',
    '/v1/system/info',
    '/v1/artifacts/report?redirect=1',
    'https://user:password@example.test/v1/artifacts/report',
  ])('rejects an out-of-boundary artifact URL before attaching credentials: %s', async (url) => {
    setApiKey('artifact-boundary-secret-canary')
    const fetchMock = vi.spyOn(globalThis, 'fetch')

    try {
      await expect(fetchArtifactBytes(url)).rejects.toThrow(ARTIFACT_READ_ERROR)
      expect(fetchMock).not.toHaveBeenCalled()
    } finally {
      fetchMock.mockRestore()
    }
  })

  it('never reads or reflects an unbounded non-success body', async () => {
    const canary = 'artifact-provider-secret-canary'
    let cancelled = false
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(canary))
      },
      cancel() {
        cancelled = true
        return new Promise<void>(() => undefined)
      },
    })
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(body, { status: 502 }))

    try {
      const outcome = await fetchArtifactBytes('/v1/artifacts/failed').catch(
        (error: unknown) => error,
      )
      expect(outcome).toBeInstanceOf(Error)
      expect((outcome as Error).message).toBe('Artifact request failed (502)')
      expect(String(outcome)).not.toContain(canary)
      expect(cancelled).toBe(true)
    } finally {
      fetchMock.mockRestore()
    }
  })
})
