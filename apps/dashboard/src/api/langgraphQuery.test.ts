import { afterEach, describe, expect, it, vi } from 'vitest'
import { setApiKey } from '@/auth/keyStorage'
import { DEFAULT_RUNTIME_CONFIG, setRuntimeConfig } from '@/config/runtimeConfig'
import { getLangGraphClient, resetLangGraphClient } from './langgraphClient'
import { canonicalNativeLangGraphRequest } from './langgraphQuery'

const ORIGIN = 'https://graph.example.test'

afterEach(() => {
  vi.restoreAllMocks()
  resetLangGraphClient()
  setRuntimeConfig(DEFAULT_RUNTIME_CONFIG)
})

describe('native LangGraph query compatibility', () => {
  it('sends the real SDK run-list and stream requests in the bounded server format', async () => {
    setRuntimeConfig({ ...DEFAULT_RUNTIME_CONFIG, langgraphOrigin: ORIGIN })
    resetLangGraphClient()
    setApiKey('transport-fixture')
    const requests: Array<{ url: URL; init?: RequestInit }> = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = new URL(input instanceof Request ? input.url : String(input))
      requests.push({ url, init })
      if (url.pathname.endsWith('/stream')) {
        return new Response('event: custom\ndata: {"kind":"phase_started"}\n\n', { headers: { 'content-type': 'text/event-stream' } })
      }
      return Response.json([{ run_id: 'run', status: 'running' }])
    })
    const controller = new AbortController()
    const client = await getLangGraphClient()
    expect(await client.runs.list('thread', { select: ['run_id', 'status'], limit: 100, signal: controller.signal }))
      .toEqual([{ run_id: 'run', status: 'running' }])
    const events = []
    for await (const event of client.runs.joinStream('thread', 'run', { streamMode: 'custom', lastEventId: 'event-2', signal: controller.signal })) events.push(event)
    expect(events).toEqual([{ event: 'custom', data: { kind: 'phase_started' } }])
    expect(requests).toHaveLength(2)
    expect(requests[0]!.url.searchParams.getAll('select')).toEqual(['run_id', 'status'])
    expect(requests[0]!.url.searchParams.get('limit')).toBe('100')
    expect(requests[1]!.url.searchParams.getAll('cancel_on_disconnect')).toEqual(['false'])
    expect(requests[1]!.url.searchParams.getAll('stream_mode')).toEqual(['custom'])
    expect(new Headers(requests[1]!.init?.headers).get('last-event-id')).toBe('event-2')
    for (const request of requests) {
      expect(new Headers(request.init?.headers).get('x-api-key')).toBe('transport-fixture')
      expect(request.init?.redirect).toBe('error')
      expect(request.init?.signal).toBe(controller.signal)
    }
  })

  it('retains Request headers, credentials and abort propagation when normalizing a GET', () => {
    const controller = new AbortController()
    const request = new Request(`${ORIGIN}/threads/thread/runs?select=%5B%22run_id%22%2C%22status%22%5D`, {
      headers: { 'x-api-key': 'request-fixture' }, credentials: 'include', signal: controller.signal,
    })
    const normalized = canonicalNativeLangGraphRequest(request, undefined, ORIGIN) as Request
    expect(new URL(normalized.url).searchParams.getAll('select')).toEqual(['run_id', 'status'])
    expect(normalized.headers.get('x-api-key')).toBe('request-fixture')
    expect(normalized.credentials).toBe('include')
    controller.abort()
    expect(normalized.signal.aborted).toBe(true)
  })

  it.each([
    ['/threads/thread/runs?select=run_id&select=status', undefined],
    ['/threads/thread/runs?select=not-json', undefined],
    ['/threads/thread/runs?select=%5B%22run_id%22%2C1%5D', undefined],
    ['/threads/thread/runs?select=%5B%22run_id%22%5D&select=status', undefined],
    ['/threads/thread/runs/run/stream?cancel_on_disconnect=1', undefined],
    ['/threads/thread/runs/run/stream?cancel_on_disconnect=true', undefined],
    ['/threads/thread/runs/run/stream?cancel_on_disconnect=0&cancel_on_disconnect=false', undefined],
    ['/v1/pipelines?select=%5B%22run_id%22%5D', undefined],
    ['/threads/thread/runs?select=%5B%22run_id%22%5D', { method: 'POST' }],
  ] as const)('leaves unsupported or ambiguous requests to server validation: %s', (path, init) => {
    const input = `${ORIGIN}${path}`
    expect(canonicalNativeLangGraphRequest(input, init, ORIGIN)).toBe(input)
  })

  it('does not rewrite a request addressed to another origin', () => {
    const input = 'https://other.example.test/threads/thread/runs?select=%5B%22run_id%22%5D'
    expect(canonicalNativeLangGraphRequest(input, undefined, ORIGIN)).toBe(input)
  })

  it('preserves duplicate fields rather than hiding an invalid selection from the server', () => {
    const input = `${ORIGIN}/threads/thread/runs?select=%5B%22run_id%22%2C%22run_id%22%5D`
    const result = canonicalNativeLangGraphRequest(input, undefined, ORIGIN)
    expect(new URL(String(result)).searchParams.getAll('select')).toEqual(['run_id', 'run_id'])
  })
})
