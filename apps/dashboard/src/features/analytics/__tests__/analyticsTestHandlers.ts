import { http, HttpResponse } from 'msw'

import type { UsageAnalytics } from '@/api/hooks/useAnalytics'

/** Daily 7d window fixture (numbers < 1000 so toLocaleString stays separator-free). */
export const USAGE_FIXTURE: UsageAnalytics = {
  window: { from: '2026-06-05T00:00:00Z', to: '2026-06-12T00:00:00Z', bucket: 'day' },
  totals: { events: 940, errors: 47, by_surface: { v1: 900, graph: 40 } },
  buckets: [
    { bucket_start: '2026-06-05T00:00:00Z', events: 120, errors: 4 },
    { bucket_start: '2026-06-06T00:00:00Z', events: 180, errors: 9 },
    { bucket_start: '2026-06-07T00:00:00Z', events: 240, errors: 12 },
    { bucket_start: '2026-06-08T00:00:00Z', events: 400, errors: 22 },
  ],
  top_actions: [
    { action: 'pipelines.list', count: 420 },
    { action: 'logs.search', count: 220 },
    { action: 'work_tracking.query.execute.translate', count: 80 },
  ],
  runs: { phases_succeeded: 42, phases_failed: 3 },
}

export const EMPTY_USAGE_FIXTURE: UsageAnalytics = {
  window: { from: '2026-06-05T00:00:00Z', to: '2026-06-12T00:00:00Z', bucket: 'day' },
  totals: { events: 0, errors: 0, by_surface: {} },
  buckets: [],
  top_actions: [],
  runs: { phases_succeeded: 0, phases_failed: 0 },
}

/** GET /v1/analytics/usage stub that captures each request's query params. */
export function usageHandler(fixture: UsageAnalytics = USAGE_FIXTURE) {
  const captured: URLSearchParams[] = []
  const handler = http.get('*/v1/analytics/usage', ({ request }) => {
    captured.push(new URL(request.url).searchParams)
    return HttpResponse.json(fixture)
  })
  return { handler, captured }
}
