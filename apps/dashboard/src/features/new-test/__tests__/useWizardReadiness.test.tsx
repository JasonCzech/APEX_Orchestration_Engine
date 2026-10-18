import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'
import { emptyDraft } from '../wizardState'
import { useWizardReadiness } from '../useWizardReadiness'
import { installWizardHandlers } from './wizardTestUtils'

describe('execution configuration verification', () => {
  it('checks only metadata identifiers and does not reuse a previous connection result', async () => {
    installWizardHandlers()
    let release!: () => void
    const hold = new Promise<void>(resolve => { release = resolve })
    const requests: Record<string, unknown>[] = []
    server.use(http.post('*/v1/pipelines/execution-readiness', async ({ request }) => {
      const body = await request.json() as Record<string, unknown>
      requests.push(body)
      if (body.connection_id === 'first') await hold
      return HttpResponse.json({ execution_selected: true, configuration_valid: body.connection_id === 'first', requires_application: false,
        detail: body.connection_id === 'first' ? null : 'The selected connection is disabled' })
    }))
    const draft = emptyDraft()
    draft.scope = { project_id: 'demo', app_id: 'app-checkout', environment_id: null }
    draft.config.golden_configurable = { connections: { execution_engine: 'first', artifact_store: 'artifacts' }, provider_options: { ignored: 'not part of metadata verification' } }
    const queryClient = createTestQueryClient()
    const view = renderHook(useWizardReadiness, { initialProps: draft, wrapper: ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider> })
    await waitFor(() => expect(requests).toHaveLength(1))
    expect(view.result.current.readiness.executionIssues[0]?.message).toContain('Verifying execution connection')
    expect(requests[0]).toEqual({ project_id: 'demo', app_id: 'app-checkout', engine: 'sim', connection_id: 'first', artifact_connection_id: 'artifacts',
      phases: ['story_analysis', 'test_planning', 'env_triage', 'script_scenario', 'execution', 'reporting', 'postmortem'] })
    view.rerender({ ...draft, config: { ...draft.config, golden_configurable: { connections: { execution_engine: 'second', artifact_store: 'artifacts' } } } })
    await waitFor(() => expect(view.result.current.readiness.executionIssues).toEqual([{ step: 'config', message: 'The selected connection is disabled' }]))
    await act(async () => { release(); await hold })
    expect(view.result.current.readiness.executionIssues).toEqual([{ step: 'config', message: 'The selected connection is disabled' }])
    view.unmount()
    queryClient.clear()
  })

  it('does not probe execution connections for a planning-only draft', async () => {
    installWizardHandlers()
    let calls = 0
    server.use(http.post('*/v1/pipelines/execution-readiness', () => { calls++; return HttpResponse.json({}) }))
    const draft = emptyDraft()
    draft.scope.project_id = 'demo'
    draft.config.phases = ['story_analysis']
    const queryClient = createTestQueryClient()
    const view = renderHook(() => useWizardReadiness(draft), { wrapper: ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider> })
    await waitFor(() => expect(view.result.current.documents.isSuccess).toBe(true))
    expect(calls).toBe(0)
    expect(view.result.current.readiness.executionIssues).toEqual([])
    view.unmount()
    queryClient.clear()
  })
})
