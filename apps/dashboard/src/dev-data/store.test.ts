import { describe, expect, it } from 'vitest'

import type { AgentAnalytics } from '@/api/hooks/useAgentAnalytics'

import { createDevDataStore } from './store'

const WIDE_WINDOW = 'from=2000-01-01T00:00:00.000Z&to=2100-01-01T00:00:00.000Z'

async function jsonOf<T>(response: Response | null): Promise<T> {
  expect(response).not.toBeNull()
  return (await (response as Response).json()) as T
}

describe('dev-data store', () => {
  it('filters and paginates pipeline summaries', async () => {
    const store = createDevDataStore()

    const body = await jsonOf<{ items: Array<{ thread_status: string; pending_gate?: unknown }>; total: number }>(
      await store.handleApexRequest(
        new Request('http://localhost/v1/pipelines?status=interrupted&limit=1&offset=0'),
      ),
    )

    expect(body.total).toBeGreaterThanOrEqual(2)
    expect(body.items).toHaveLength(1)
    expect(body.items[0]?.thread_status).toBe('interrupted')
    expect(body.items[0]?.pending_gate).toBeTruthy()
  })

  it('keeps create/delete mutations inside the in-memory session', async () => {
    const store = createDevDataStore()
    const createResponse = await store.handleApexRequest(
      new Request('http://localhost/v1/work-tracking/saved-queries', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          name: 'Fresh dummy query',
          provider: 'jira',
          query: 'project = PHX',
          project_id: 'proj-alpha',
        }),
      }),
    )
    const created = await jsonOf<{ id: string; name: string }>(createResponse)

    const list = await jsonOf<{ items: Array<{ id: string; name: string }> }>(
      await store.handleApexRequest(new Request('http://localhost/v1/work-tracking/saved-queries')),
    )
    expect(list.items.some((item) => item.id === created.id && item.name === 'Fresh dummy query')).toBe(
      true,
    )

    const deleteResponse = await store.handleApexRequest(
      new Request(`http://localhost/v1/work-tracking/saved-queries/${created.id}`, {
        method: 'DELETE',
      }),
    )
    expect(deleteResponse?.status).toBe(204)
  })

  it('serves local artifact bytes', async () => {
    const store = createDevDataStore()

    const bytes = store.getArtifactBytes('http://localhost/v1/artifacts/reports/exec-report')

    expect(bytes?.mediaType).toBe('application/json')
    expect(await bytes?.blob.text()).toContain('apexload')
  })

  it('returns a development problem for unhandled v1 routes', async () => {
    const store = createDevDataStore()

    const response = await store.handleApexRequest(new Request('http://localhost/v1/nope'))
    const body = await jsonOf<{ title: string }>(response)

    expect(response?.status).toBe(501)
    expect(body.title).toBe('dummy_handler_missing')
  })

  it('serves internally-consistent agent analytics that honor group_by and filters', async () => {
    const store = createDevDataStore()
    const byStage = await jsonOf<AgentAnalytics>(
      await store.handleApexRequest(
        new Request(`http://localhost/v1/analytics/agents?group_by=stage&limit=50&${WIDE_WINDOW}`),
      ),
    )

    expect(byStage.cost_visible).toBe(true)
    expect(byStage.breakdown.length).toBeGreaterThan(0)
    // A full-page breakdown partitions every event, so its token sums reconcile
    // exactly with the totals.
    const tokenSum = byStage.breakdown.reduce((sum, row) => sum + row.total_tokens, 0)
    expect(tokenSum).toBe(byStage.totals.total_tokens)

    const byAgent = await jsonOf<AgentAnalytics>(
      await store.handleApexRequest(
        new Request(`http://localhost/v1/analytics/agents?group_by=agent&limit=50&${WIDE_WINDOW}`),
      ),
    )
    expect(byAgent.breakdown.every((row) => row.key.endsWith('.worker'))).toBe(true)

    // A status filter narrows the result set.
    const errorsOnly = await jsonOf<AgentAnalytics>(
      await store.handleApexRequest(
        new Request(`http://localhost/v1/analytics/agents?group_by=stage&status=error&${WIDE_WINDOW}`),
      ),
    )
    expect(errorsOnly.totals.events).toBeLessThanOrEqual(byStage.totals.events)
    expect(errorsOnly.totals.errors).toBe(errorsOnly.totals.events)
  })
})

