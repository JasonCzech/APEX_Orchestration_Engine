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
const tab = (name: RegExp) => screen.getByRole('tab', { name })
async function openTab(name: RegExp) {
  await userEvent.click(tab(name))
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
    await openTab(/Story Analysis/)
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
    await user.click(tab(/Story Analysis/))
    const phase = screen.getByLabelText('Phase context — Story Analysis')
    expect(within(phase).queryByRole('option', { name: /planning-default/ })).not.toBeInTheDocument()
    await user.selectOptions(phase, 'analysis-alternative')
    await user.click(tab(/Application context/))
    const app = screen.getByLabelText('Add Application context')
    expect(within(app).queryByRole('option', { name: 'app-hidden' })).not.toBeInTheDocument()
    await user.selectOptions(app, 'app-one')
    await user.selectOptions(app, 'app-shared')
    await user.click(tab(/Environment Context/))
    await user.selectOptions(screen.getByLabelText('Add Environment Context'), 'env-one')
    expect(view.draft().prompt_selections?.application.map(item => item.prompt_id)).toEqual(['app-one', 'app-shared'])
    await user.click(tab(/Application context/))
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
    await screen.findByRole('option', { name: 'system-alternative' })
    await user.click(tab(/Story Analysis/))
    await user.selectOptions(screen.getByLabelText('Phase context — Story Analysis'), 'analysis-alternative')
    await user.click(screen.getByRole('button', { name: 'Include planning' }))
    await waitFor(() => expect(view.draft().prompt_selections?.phases.test_planning?.prompt_id).toBe('planning-default'))
    expect(view.draft().prompt_selections?.phases.story_analysis?.prompt_id).toBe('analysis-alternative')
    expect(tab(/Test Planning/)).toHaveTextContent('planning-default')
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
  it('presents each prompt slot as a tab in pipeline order with its selection and status', async () => {
    server.use(...handlers())
    const initial = emptyDraft()
    initial.config.phases = ['story_analysis', 'test_planning']
    initial.scope = { project_id: 'project-one', app_id: 'app-checkout', environment_id: 'env-staging' } as WizardDraft['scope']
    setup(initial)
    await waitFor(() => expect(tab(/System prompt/)).toHaveTextContent('system-default'))
    // Shared context sits under the system prompt; every phase follows in pipeline order.
    expect(screen.getAllByRole('tab').map(item => item.textContent)).toEqual([
      expect.stringContaining('System prompt'), expect.stringContaining('Application context'), expect.stringContaining('Environment Context'),
      expect.stringContaining('Story Analysis'), expect.stringContaining('Test Planning'), expect.stringContaining('Env Triage'),
      expect.stringContaining('Script & Scenario'), expect.stringContaining('Execution'), expect.stringContaining('Reporting & Analysis'),
      expect.stringContaining('Postmortem'),
    ])
    expect(tab(/Story Analysis/)).toHaveTextContent('analysis-default')
    expect(tab(/Env Triage/)).toHaveTextContent('Person · bypassed')
    expect(tab(/Application context/)).toHaveTextContent('None')
    expect(tab(/System prompt/)).toHaveAttribute('aria-selected', 'true')
    expect(screen.queryByLabelText('Phase context — Story Analysis')).not.toBeInTheDocument()
    tab(/System prompt/).focus()
    await userEvent.keyboard('{ArrowDown}')
    expect(tab(/Application context/)).toHaveFocus()
    expect(tab(/Application context/)).toHaveAttribute('aria-selected', 'true')
    await userEvent.click(tab(/Story Analysis/))
    expect(screen.getByRole('tabpanel', { name: /Story Analysis/ })).toHaveTextContent('phase 1 of 7')
    tab(/Story Analysis/).focus()
    await userEvent.keyboard('{End}')
    expect(tab(/Postmortem/)).toHaveFocus()
    await userEvent.click(screen.getByRole('button', { name: /← Reporting & Analysis/ }))
    expect(tab(/Reporting & Analysis/)).toHaveAttribute('aria-selected', 'true')
  })
  it('shows person-run phases as bypassed and switches ownership both ways', async () => {
    server.use(...handlers())
    const view = setup()
    const user = userEvent.setup()
    await waitFor(() => expect(view.draft().prompt_selection_issues).toEqual([]))
    await user.click(tab(/Test Planning/))
    const panel = () => screen.getByRole('tabpanel', { name: /Test Planning/ })
    expect(panel()).toHaveTextContent('Bypassed for agentic execution')
    expect(panel()).toHaveTextContent('the catalog default context is selected')
    expect(screen.queryByLabelText('Phase context — Test Planning')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Run this phase with an agent' }))
    expect(view.draft().config.phases).toEqual(['story_analysis', 'test_planning'])
    await waitFor(() => expect(view.draft().prompt_selections?.phases.test_planning?.prompt_id).toBe('planning-default'))
    expect(screen.getByLabelText('Phase context — Test Planning')).toHaveValue('planning-default')
    expect(tab(/Test Planning/)).toHaveAttribute('aria-selected', 'true')

    await user.click(screen.getByRole('button', { name: 'Bypass: a person completes this phase' }))
    expect(view.draft().config.phases).toEqual(['story_analysis'])
    expect(screen.getByRole('heading', { name: 'Test Planning' })).toHaveFocus()
    expect(screen.getByRole('status')).toHaveTextContent('Test Planning is now completed by a person')
    expect(tab(/Test Planning/)).toHaveTextContent('Person · bypassed')
    // The saved context is kept for later, shown read-only, and left out of the launch.
    expect(view.draft().prompt_selections?.phases.test_planning?.prompt_id).toBe('planning-default')
    expect(await within(panel()).findByLabelText('planning-default content')).toHaveTextContent('Content for planning-default')
    expect(within(panel()).queryByRole('button', { name: 'Override for this test' })).not.toBeInTheDocument()
    expect(buildConfigurable(view.draft()).prompt_selections).toMatchObject({ phases: { story_analysis: expect.anything() } })
    expect((buildConfigurable(view.draft()).prompt_selections as { phases: object }).phases).not.toHaveProperty('test_planning')
  })
  it('shows the phase context beside the prompt the runtime assembles from it', async () => {
    server.use(...handlers())
    const view = setup()
    const user = userEvent.setup()
    await waitFor(() => expect(view.draft().prompt_selection_issues).toEqual([]))
    await user.click(tab(/Application context/))
    await user.selectOptions(screen.getByLabelText('Add Application context'), 'app-one')
    await user.click(tab(/Environment Context/))
    await user.selectOptions(screen.getByLabelText('Add Environment Context'), 'env-one')
    await user.click(tab(/Story Analysis/))
    const region = screen.getByRole('region', { name: 'Assembled prompt for Story Analysis' })
    const assembled = () => region.querySelector('pre.wizard-prompt-content:not(details pre)')!
    await waitFor(() => expect(assembled()).toHaveTextContent('Content for env-one'))
    const text = assembled().textContent ?? ''
    // Same order as compose_selected_system: system, phase, then application and environment context.
    const order = ['=== SHARED SYSTEM PROMPT ===', 'Content for system-default', '=== PHASE CONTEXT ===', 'Content for analysis-default',
      '=== APPLICATION CONTEXT: app-one ===', 'Content for app-one', '=== ENVIRONMENT CONTEXT: env-one ===', 'Content for env-one'].map(part => text.indexOf(part))
    expect(order.every(index => index >= 0)).toBe(true)
    expect(order).toEqual([...order].sort((a, b) => a - b))
    expect(within(region).queryByRole('button', { name: 'Override for this test' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Override for this test' }))
    const editor = screen.getByLabelText('Override analysis-default')
    await user.clear(editor)
    await user.type(editor, 'Edited phase context')
    expect(assembled()).toHaveTextContent('=== PHASE CONTEXT === Edited phase context')
  })
  it('adds the runtime scope line to each context and never shows a stuck loading state', async () => {
    server.use(http.get('*/v1/prompts/archived/versions/:version', () => HttpResponse.json({ detail: 'Not found' }, { status: 404 })), ...handlers())
    const initial = emptyDraft()
    initial.config.phases = ['story_analysis', 'test_planning']
    initial.scope = { project_id: 'project-one', app_id: 'app-checkout', environment_id: 'env-staging', test_scope: {
      schema_version: 1, primary_participant_id: 'be', traffic_entry_participant_id: 'be',
      participants: [{ id: 'be', layer: 'backend', project_id: 'project-one', application_id: 'app-checkout', environment_id: 'env-staging' }],
    } } as WizardDraft['scope']
    initial.prompt_selections!.application = [{ prompt_id: 'app-one', version_id: 'app-one-v1' }, { prompt_id: 'app-shared', version_id: 'app-shared-v1' }]
    initial.prompt_selections!.phases.test_planning = { prompt_id: 'archived', version_id: 'old' }
    setup(initial)
    const user = userEvent.setup()
    await waitFor(() => expect(tab(/System prompt/)).toHaveTextContent('system-default'))
    await user.click(tab(/Story Analysis/))
    const region = screen.getByRole('region', { name: 'Assembled prompt for Story Analysis' })
    await waitFor(() => expect(region).toHaveTextContent('=== APPLICATION CONTEXT: app-one === Applies to participants: be (backend, primary system, traffic entry) Content for app-one'))
    expect(region).toHaveTextContent('=== APPLICATION CONTEXT: app-shared === Shared across all test participants Content for app-shared')
    // An unavailable phase prompt is reported, not shown as loading.
    await user.click(tab(/Test Planning/))
    const planning = screen.getByRole('region', { name: 'Assembled prompt for Test Planning' })
    await waitFor(() => expect(planning).toHaveTextContent("A selected prompt could not be loaded"))
    expect(within(planning).queryByRole('status')).not.toBeInTheDocument()
  })
  it('offers to run a phase with an agent when every phase is assigned to a person', async () => {
    server.use(...handlers())
    const initial = emptyDraft()
    initial.config.phases = []
    const view = setup(initial)
    // The visible label is part of the accessible name (WCAG 2.5.3).
    await userEvent.click(await screen.findByRole('button', { name: 'Run with an agent: Story Analysis' }))
    expect(view.draft().config.phases).toEqual(['story_analysis'])
    await waitFor(() => expect(tab(/Story Analysis/)).toHaveTextContent('analysis-default'))
    expect(tab(/Story Analysis/)).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('heading', { name: 'Story Analysis' })).toHaveFocus()
    expect(screen.getByRole('status')).toHaveTextContent('Story Analysis is now run by an agent.')
  })
  it('opens on the first slot that needs attention and flags it in the tab', async () => {
    server.use(...handlers())
    const initial = emptyDraft()
    initial.config.phases = ['story_analysis']
    initial.prompt_selections!.phases.story_analysis = { prompt_id: 'archived', version_id: 'old' }
    setup(initial)
    await waitFor(() => expect(tab(/Story Analysis/)).toHaveAttribute('aria-selected', 'true'))
    expect(tab(/Story Analysis/)).toHaveTextContent('Needs attention')
    expect(screen.getByRole('tabpanel')).toHaveTextContent('A selected prompt is unavailable. Choose a replacement.')
    await userEvent.selectOptions(screen.getByLabelText('Phase context — Story Analysis'), 'analysis-default')
    await waitFor(() => expect(tab(/Story Analysis/)).not.toHaveTextContent('Needs attention'))
    expect(tab(/Story Analysis/)).toHaveAttribute('aria-selected', 'true')
  })
})
