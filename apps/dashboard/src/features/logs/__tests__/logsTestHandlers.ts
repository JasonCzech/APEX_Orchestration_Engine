import { http, HttpResponse } from 'msw'

import type { LogEntry, LogSearchResponse } from '@/api/hooks/useLogs'

/** Body shape the page POSTs to /v1/logs/search (captured for assertions). */
export interface CapturedLogSearch {
  query?: { text?: string; filters?: Record<string, string> }
  window?: { from?: string | null; to?: string | null }
  limit: number
  offset: number
}

/** One entry per level tone, plus provider extras on the ERROR line. */
export const LOG_ENTRIES: LogEntry[] = [
  {
    at: '2026-06-12T10:00:00Z',
    level: 'ERROR',
    service: 'apex-api',
    message: 'phase execution failed: engine timeout',
    fields: { thread_id: 'run-123', attempt: 2 },
  },
  {
    at: '2026-06-12T09:59:30Z',
    level: 'WARN',
    service: 'apex-worker',
    message: 'retrying artifact upload',
    fields: {},
  },
  {
    at: '2026-06-12T09:59:00Z',
    level: 'INFO',
    service: 'apex-api',
    message: 'pipeline started',
    fields: {},
  },
  {
    at: '2026-06-12T09:58:00Z',
    level: 'DEBUG',
    service: 'apex-worker',
    message: 'polling engine status',
    fields: {},
  },
]

export function makeEntries(count: number, startIndex = 0): LogEntry[] {
  return Array.from({ length: count }, (_, i) => ({
    at: `2026-06-12T0${(startIndex + i) % 10}:00:00Z`,
    level: 'INFO',
    service: 'apex-api',
    message: `log line ${startIndex + i}`,
    fields: {},
  }))
}

/**
 * POST /v1/logs/search stub: captures each body and answers with a slice of
 * `all` at the requested offset (offset pagination), echoing the window.
 */
export function logsHandler(all: LogEntry[] = LOG_ENTRIES) {
  const captured: CapturedLogSearch[] = []
  const handler = http.post('*/v1/logs/search', async ({ request }) => {
    const body = (await request.json()) as CapturedLogSearch
    captured.push(body)
    const response: LogSearchResponse = {
      entries: all.slice(body.offset, body.offset + body.limit),
      total: all.length,
      limit: body.limit,
      offset: body.offset,
      window: {
        from: body.window?.from ?? '2026-06-12T09:00:00Z',
        to: body.window?.to ?? '2026-06-12T10:00:00Z',
      },
    }
    return HttpResponse.json(response)
  })
  return { handler, captured }
}

/** Non-2xx stub (422 query rejection / 502 upstream failure problems). */
export function logsErrorHandler(status: number, detail: string) {
  const captured: CapturedLogSearch[] = []
  const handler = http.post('*/v1/logs/search', async ({ request }) => {
    captured.push((await request.json()) as CapturedLogSearch)
    return HttpResponse.json({ detail }, { status })
  })
  return { handler, captured }
}
