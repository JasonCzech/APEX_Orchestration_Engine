import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { getApexClient } from '@/api/apexClient'
import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'
import { buildConfigurable, parseDraftPayload } from '@/features/new-test/wizardState'
import { server } from '@/test/server'
import { CopyTestSetupButton } from '../CopyTestSetupButton'

function Location() { const location = useLocation(); return <output>{location.pathname}{location.search}</output> }
function setup() { render(<MemoryRouter><CopyTestSetupButton threadId="original" /><Location /></MemoryRouter>) }

describe('copy test setup', () => {
  it('creates an editable draft with pinned references and redirects to prompt validation', async () => {
    const selections = { schema_version: 1, system: { prompt_id: 'system', version_id: 'system-v2' }, phases: { story_analysis: { prompt_id: 'phase', version_id: 'phase-v1', override_content: 'Customized phase' } }, application: [], environment: [] }
    let saved: Record<string, unknown> | undefined
    server.use(
      http.get('*/v1/pipelines/original/setup', () => HttpResponse.json({ title: 'Copied test', request: 'Analyze checkout', project_id: 'project', app_id: null, environment_id: null, configurable: { engine: 'sim', phases: ['story_analysis'], gates: {}, prompt_selections: selections } })),
      http.post('*/v1/drafts', async ({ request }) => { saved = await request.json() as Record<string, unknown>; return HttpResponse.json({ id: 'copy-draft' }) }),
    )
    setup()
    await userEvent.click(screen.getByRole('button', { name: 'Create new test from this run' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('/runs/new?draft=copy-draft&step=scope'))
    expect(saved).toMatchObject({ project_id: 'project', payload: { prompt_selections: selections, prompt_selection_issues: ['Validating prompt selections…'] } })
    expect(JSON.stringify(saved)).not.toContain('prompt_manifest')
  })
  it('surfaces an unavailable historical setup without creating a draft', async () => {
    server.use(http.get('*/v1/pipelines/original/setup', () => HttpResponse.json({ detail: 'The stored setup cannot be copied safely' }, { status: 409 })))
    setup()
    await userEvent.click(screen.getByRole('button', { name: 'Create new test from this run' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('cannot be copied safely')
  })
})


it('does not create a draft for a run that was left while its setup was loading', async () => {
  const client = getApexClient()
  let release: ((value: unknown) => void) | undefined
  const pending = new Promise(resolve => { release = resolve })
  const get = vi.spyOn(client, 'GET').mockReturnValueOnce(pending as ReturnType<typeof client.GET>)
  let saved = false
  server.use(http.post('*/v1/drafts', () => { saved = true; return HttpResponse.json({ id: 'wrong-draft' }) }))
  const view = render(<MemoryRouter><CopyTestSetupButton threadId="original" /><Location /></MemoryRouter>)
  await userEvent.click(screen.getByRole('button', { name: 'Create new test from this run' }))
  expect(get).toHaveBeenCalledOnce()
  view.rerender(<MemoryRouter><CopyTestSetupButton threadId="different-run" /><Location /></MemoryRouter>)
  await act(async () => {
    release!({ data: { title: 'Original test', request: 'Analyze', project_id: 'project', configurable: { engine: 'sim', phases: ['story_analysis'] } }, response: new Response('{}') })
    await pending
  })
  expect(screen.getByRole('button', { name: 'Create new test from this run' })).toBeEnabled()
  expect(saved).toBe(false)
  expect(screen.getByRole('status')).not.toHaveTextContent('/runs/new')
  get.mockRestore()
})


it.each<{ agentPhases: PhaseName[] }>([{ agentPhases: ['story_analysis', 'execution'] }, { agentPhases: [] }])('preserves copied phase ownership and analysis configuration: $agentPhases', async ({ agentPhases }) => {
  const owners = Object.fromEntries(PHASE_NAMES.map(phase => [phase, agentPhases.includes(phase) ? 'langgraph' : 'hitl']))
  const analysis = { schema_version: 1, selections: [{ tool: 'internal_rca', connection_id: 'rca-connection' }] }
  let saved: Record<string, unknown> | undefined
  server.use(
    http.get('*/v1/pipelines/original/setup', () => HttpResponse.json({
      title: 'Human and agent test', request: 'Analyze checkout', project_id: 'project', app_id: 'app', environment_id: 'env',
      configurable: { engine: 'apex_load', phases: PHASE_NAMES, phase_execution: { schema_version: 1, owners }, analysis, gates: {} },
    })),
    http.post('*/v1/drafts', async ({ request }) => { saved = await request.json() as Record<string, unknown>; return HttpResponse.json({ id: 'copy-owned-draft' }) }),
  )
  setup()
  await userEvent.click(screen.getByRole('button', { name: 'Create new test from this run' }))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('copy-owned-draft'))
  const configurable = buildConfigurable(parseDraftPayload(saved?.payload))
  expect(configurable.phase_execution).toEqual({ schema_version: 1, owners })
  expect(configurable.analysis).toEqual(analysis)
  expect(configurable.phases).toEqual(PHASE_NAMES)
})
