import { useState } from 'react'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { server } from '@/test/server'
import { PromptsStep } from '../steps/PromptsStep'
import { legacyLaunchIntent } from '../legacyLaunchIntent'
import { emptyDraft, buildConfigurable, parseDraftPayload, type WizardDraft } from '../wizardState'

vi.mock('@uiw/react-codemirror', async () => {
  const { createElement } = await import('react')
  return { default: ({ value, onChange, 'aria-label': label }: { value: string; onChange?: (value: string) => void; 'aria-label'?: string }) => createElement('textarea', { value, 'aria-label': label, onChange: (e: { target: { value: string } }) => onChange?.(e.target.value) }) }
})
const row = (id: string, namespace: string, metadata = {}) => ({ id, namespace, key: id, selection_enabled: true, is_shared: false, is_default: false, active_version: { id: `${id}-v1`, version: 1 }, ...metadata })
const rows = [
  row('system-default', 'system', { is_default: true }), row('system-alternative', 'system'),
  row('analysis-default', 'phase', { phase: 'story_analysis', is_default: true }), row('analysis-alternative', 'phase', { phase: 'story_analysis' }),
  row('planning-default', 'phase', { phase: 'test_planning', is_default: true }),
  row('app-one', 'application', { application_id: 'app-checkout' }), row('app-shared', 'application', { is_shared: true }), row('app-hidden', 'application', { application_id: 'app-other' }),
  row('env-one', 'environment', { environment_id: 'env-staging' }), row('env-hidden', 'environment', { environment_id: 'env-other' }),
]
function handlers() {
  return [http.get('*/v1/prompts', () => HttpResponse.json(rows)), http.get('*/v1/prompts/:id/versions/:version', ({ params }) => HttpResponse.json({ id: params.version, version: 1, content: `Content for ${params.id}`, note: null }))]
}
function setup(initial?: WizardDraft) {
  let latest = initial ?? { ...emptyDraft(), config: { ...emptyDraft().config, phases: ['story_analysis'] as const }, scope: { project_id: 'project-one', app_id: 'app-checkout', environment_id: 'env-staging' } } as WizardDraft
  function Harness() {
    const [draft, setDraft] = useState(latest)
    latest = draft
    return <><PromptsStep draft={draft} onChange={setDraft} /><button onClick={() => setDraft(current => ({ ...current, scope: { ...current.scope, app_id: 'app-other', environment_id: 'env-other' } }))}>Change scope</button><button onClick={() => setDraft(current => ({ ...current, config: { ...current.config, phases: ['story_analysis', 'test_planning'] } }))}>Include planning</button></>
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const result = render(<QueryClientProvider client={client}><Harness /></QueryClientProvider>)
  return { ...result, draft: () => latest }
}

describe('catalog prompt selections', () => {
  it('preselects one system and one context per included phase; sends pinned IDs', async () => {
    server.use(...handlers())
    const view = setup()
    await waitFor(() => expect(view.draft().prompt_selection_issues).toEqual([]))
    expect(screen.getByLabelText('System Prompt')).toHaveValue('system-default')
    expect(screen.getByLabelText('Phase context — Story Analysis')).toHaveValue('analysis-default')
    const payload = buildConfigurable(view.draft())
    expect(payload.prompt_overrides).toBeUndefined()
    expect(payload.prompt_selections).toMatchObject({ system: { prompt_id: 'system-default', version_id: 'system-default-v1' }, application: [], environment: [] })
    expect(parseDraftPayload(view.draft()).prompt_selections).toEqual(view.draft().prompt_selections)
  })
  it('filters alternatives by phase and scopes contexts; preserves optional ordering', async () => {
    server.use(...handlers())
    const view = setup()
    const user = userEvent.setup()
    await screen.findByRole('option', { name: 'system-alternative' })
    await user.selectOptions(screen.getByLabelText('System Prompt'), 'system-alternative')
    const phase = screen.getByLabelText('Phase context — Story Analysis')
    expect(within(phase).queryByRole('option', { name: /planning-default/ })).not.toBeInTheDocument()
    await user.selectOptions(phase, 'analysis-alternative')
    const app = screen.getByLabelText('Add Application context')
    expect(within(app).queryByRole('option', { name: 'app-hidden' })).not.toBeInTheDocument()
    await user.selectOptions(app, 'app-one')
    await user.selectOptions(app, 'app-shared')
    await user.selectOptions(screen.getByLabelText('Add Environment Context'), 'env-one')
    expect(view.draft().prompt_selections?.application.map(item => item.prompt_id)).toEqual(['app-one', 'app-shared'])
    await user.click(within(screen.getByRole('region', { name: 'Application context' })).getAllByRole('button', { name: 'Move up' })[1]!)
    expect(view.draft().prompt_selections?.application.map(item => item.prompt_id)).toEqual(['app-shared', 'app-one'])
    await user.click(screen.getByRole('button', { name: 'Change scope' }))
    await waitFor(() => expect(view.draft().prompt_selections?.application.map(item => item.prompt_id)).toEqual(['app-shared']))
    expect(view.draft().prompt_selections?.environment).toEqual([])
    expect(screen.getByRole('status')).toHaveTextContent('were removed')
  })
  it('adds defaults for newly included phases and retains existing alternatives', async () => {
    server.use(...handlers())
    const view = setup()
    const user = userEvent.setup()
    await screen.findByRole('option', { name: 'analysis-alternative' })
    await user.selectOptions(screen.getByLabelText('Phase context — Story Analysis'), 'analysis-alternative')
    await user.click(screen.getByRole('button', { name: 'Include planning' }))
    await waitFor(() => expect(view.draft().prompt_selections?.phases.test_planning?.prompt_id).toBe('planning-default'))
    expect(view.draft().prompt_selections?.phases.story_analysis?.prompt_id).toBe('analysis-alternative')
  })
  it('does not silently replace an unavailable selected prompt', async () => {
    server.use(...handlers())
    const initial = emptyDraft()
    initial.config.phases = ['story_analysis']
    initial.prompt_selections!.system = { prompt_id: 'archived', version_id: 'old' }
    const view = setup(initial)
    await waitFor(() => expect(view.draft().prompt_selection_issues?.some(issue => issue.includes('unavailable'))).toBe(true))
    expect(view.draft().prompt_selections?.system?.prompt_id).toBe('archived')
  })
  it('keeps legacy user overrides visible and blocks until explicitly resolved', async () => {
    server.use(...handlers())
    const initial = emptyDraft()
    initial.config.phases = ['story_analysis']
    initial.prompt_overrides = { 'phase/story_analysis': { content: 'Migrated phase instructions' }, 'phase/story_analysis/user': { content: 'Retain this custom request template' } }
    const priorIntent = legacyLaunchIntent(initial)
    const view = setup(initial)
    await waitFor(() => expect(view.draft().prompt_selections?.phases.story_analysis?.override_content).toBe('Migrated phase instructions'))
    expect(legacyLaunchIntent(view.draft())).toEqual(priorIntent)
    expect(legacyLaunchIntent(parseDraftPayload(JSON.parse(JSON.stringify(view.draft()))))).toEqual(priorIntent)
    expect(buildConfigurable(view.draft()).legacy_prompt_overrides).toBeUndefined()
    expect(screen.getByText('Retain this custom request template')).toBeVisible()
    expect(view.draft().prompt_selection_issues).toContain('Resolve the retained legacy prompt overrides before launching.')
    await userEvent.click(screen.getByRole('button', { name: 'Remove legacy override phase/story_analysis/user' }))
    await waitFor(() => expect(view.draft().prompt_selection_issues).toEqual([]))
  })
  it('fails closed when the catalog cannot load', async () => {
    server.use(http.get('*/v1/prompts', () => HttpResponse.json({ detail: 'Unavailable' }, { status: 503 })))
    const view = setup()
    await screen.findByRole('alert')
    expect(view.draft().prompt_selections?.system).toBeNull()
    await waitFor(() => expect(view.draft().prompt_selection_issues).toContain('Prompt catalog could not be loaded. Retry before launching.'))
  })
})
