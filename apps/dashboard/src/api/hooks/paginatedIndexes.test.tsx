import type { ReactNode } from 'react'

import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { server } from '@/test/server'

import { useApplications, useEnvironments } from './useCatalog'
import { useConnectionsIndex } from './useConnections'
import { useConsumersIndex } from './useConsumers'
import { useEvidence } from './useContextApi'
import {
  useApplicationsIndex,
  useEnvironmentsIndex,
} from './useEnvironments'

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}

function page(prefix: string, offset: number, pageSize: number): Array<{ id: string }> {
  if (offset === 0) {
    return Array.from({ length: pageSize }, (_, index) => ({ id: `${prefix}-${index}` }))
  }
  return offset === pageSize ? [{ id: `${prefix}-${offset}` }] : []
}

describe('complete index hooks', () => {
  it('walks every backend page for admin, catalog, wizard, and evidence lists', async () => {
    const seen = new Map<string, number[]>()
    const capture = (key: string, url: URL): number => {
      const offset = Number(url.searchParams.get('offset'))
      seen.set(key, [...(seen.get(key) ?? []), offset])
      return offset
    }
    server.use(
      http.get('*/v1/admin/consumers', ({ request }) => {
        const url = new URL(request.url)
        return HttpResponse.json(page('consumer', capture('consumers', url), 200))
      }),
      http.get('*/v1/admin/connections', ({ request }) => {
        const url = new URL(request.url)
        return HttpResponse.json(page('connection', capture('connections', url), 200))
      }),
      http.get('*/v1/catalog/applications', ({ request }) => {
        const url = new URL(request.url)
        const key = url.searchParams.has('project') ? 'wizard-applications' : 'applications'
        return HttpResponse.json(page(key, capture(key, url), 200))
      }),
      http.get('*/v1/catalog/environments', ({ request }) => {
        const url = new URL(request.url)
        const key = url.searchParams.has('application') ? 'wizard-environments' : 'environments'
        return HttpResponse.json(page(key, capture(key, url), 200))
      }),
      http.get('*/v1/context/evidence', ({ request }) => {
        const url = new URL(request.url)
        return HttpResponse.json(page('evidence', capture('evidence', url), 100))
      }),
    )

    const { result } = renderHook(
      () => ({
        consumers: useConsumersIndex(),
        connections: useConnectionsIndex(),
        applications: useApplicationsIndex(),
        environments: useEnvironmentsIndex(),
        wizardApplications: useApplications('proj-alpha'),
        wizardEnvironments: useEnvironments('app-checkout'),
        evidence: useEvidence('proj-alpha'),
      }),
      { wrapper },
    )

    await waitFor(() => {
      expect(Object.values(result.current).every((query) => query.isSuccess)).toBe(true)
    })
    expect(result.current.consumers.data).toHaveLength(201)
    expect(result.current.connections.data).toHaveLength(201)
    expect(result.current.applications.data).toHaveLength(201)
    expect(result.current.environments.data).toHaveLength(201)
    expect(result.current.wizardApplications.data).toHaveLength(201)
    expect(result.current.wizardEnvironments.data).toHaveLength(201)
    expect(result.current.evidence.data).toHaveLength(101)
    expect(Object.fromEntries(seen)).toEqual({
      consumers: [0, 200],
      connections: [0, 200],
      applications: [0, 200],
      environments: [0, 200],
      'wizard-applications': [0, 200],
      'wizard-environments': [0, 200],
      evidence: [0, 100],
    })
  })
})
