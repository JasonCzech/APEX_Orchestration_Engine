/**
 * Config section: phase-subset toggles with blocking dependency errors, the
 * gates segmented control + custom matrix, and organization presets (applied
 * from the wizard's preset bar) pre-filling engine/gates.
 */
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { emptyDraft } from '../wizardState'

import { draftRead, fillScope, installWizardHandlers, renderWizard } from './wizardTestUtils'

const { assistantsSearch, assistantsGet } = vi.hoisted(() => ({
  assistantsSearch: vi.fn(),
  assistantsGet: vi.fn(),
}))

vi.mock('@/api/langgraphClient', () => ({
  getLangGraphClient: () =>
    Promise.resolve({
      assistants: { search: assistantsSearch, get: assistantsGet },
    }),
}))

const GOLDEN = {
  assistant_id: 'asst-gold',
  graph_id: 'pipeline',
  name: 'Nightly checkout soak',
  description: 'Pinned engine + auto gates',
  config: {
    configurable: {
      engine: 'loadrunner',
      gates: { execution: { prompt_review: 'auto', output_review: 'auto' } },
    },
  },
  context: {},
  metadata: { created_by: 'dash-ops' },
  created_at: '2026-06-01T00:00:00Z',
  updated_at: '2026-06-01T00:00:00Z',
  version: 1,
}

const SYSTEM_DEFAULT = { ...GOLDEN, assistant_id: 'asst-sys', name: 'pipeline', metadata: { created_by: 'system' } }
const DEFAULTED = {
  ...GOLDEN,
  assistant_id: 'asst-defaulted',
  name: 'Baseline defaults',
  description: 'Uses PipelineConfigurable defaults',
  config: { configurable: {} },
}

async function applyFromBar(user: ReturnType<typeof userEvent.setup>, open: string, name: string) {
  await user.click(screen.getByRole('button', { name: open }))
  await user.click(await screen.findByRole('button', { name: `Choose: ${name}` }))
  await user.click(within(await screen.findByRole('dialog', { name: `Apply ${name}` })).getByRole('button', { name: 'Apply preset' }))
}

describe('ConfigStep', () => {
  it('uses a single tab stop and moves engine selection with the arrow keys', async () => {
    assistantsSearch.mockResolvedValue([])
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard()
    await user.click(screen.getByRole('tab', { name: 'Config' }))
    const group = screen.getByRole('radiogroup', { name: 'Execution engine' })
    const radios = within(group).getAllByRole('radio')
    expect(radios.filter(radio => radio.tabIndex === 0)).toHaveLength(1)
    radios[0]!.focus()
    await user.keyboard('{ArrowRight}')
    expect(radios[1]).toHaveFocus()
    expect(radios[1]).toHaveAttribute('aria-checked', 'true')
    expect(radios[0]).toHaveAttribute('tabindex', '-1')
    await user.keyboard('{Home}{ArrowLeft}')
    expect(radios.at(-1)).toHaveFocus()
    expect(radios.at(-1)).toHaveAttribute('aria-checked', 'true')
    rendered.unmount()
  })

  it('assigns deselected phases to a person without removing prerequisites', async () => {
    assistantsSearch.mockResolvedValue([])
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard()

    await fillScope(user, screen)
    await user.click(screen.getByRole('tab', { name: 'Config' }))

    const strip = await screen.findByRole('group', { name: 'Phase execution' })
    const toggles = within(strip).getAllByRole('button')
    expect(toggles).toHaveLength(7)
    for (const toggle of toggles) expect(toggle).toHaveAttribute('aria-pressed', 'true')

    // Phase selection only changes the plan; prompt selection is managed in Prompts.
    await user.click(within(strip).getByRole('button', { name: 'Execution' }))
    expect(within(strip).getByRole('button', { name: 'Execution' })).toHaveAttribute('aria-pressed', 'false')
    expect(within(strip).getByRole('button', { name: 'Execution' })).toHaveTextContent('Person')
    expect(screen.queryByTestId('phase-dependency-hints')).not.toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    expect(screen.getByRole('button', { name: /^Execution: Person, bypassed for agentic execution/ })).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: 'Config' }))

    // Toggling execution back on clears the hint (all-7 = null subset again).
    await user.click(within(strip).getByRole('button', { name: 'Execution' }))
    await waitFor(() =>
      expect(screen.queryByTestId('phase-dependency-hints')).not.toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await screen.findByText('Draft saved')
    rendered.unmount()
  })

  it('keeps an external execution test blocked until its target is set', async () => {
    assistantsSearch.mockResolvedValue([])
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard()

    await fillScope(user, screen)
    await user.click(screen.getByRole('button', { name: 'Clear traffic entry' }))
    await user.click(screen.getByRole('tab', { name: 'Config' }))
    await user.click(screen.getByRole('radio', { name: /APEX Load/ }))
    await user.click(screen.getByRole('tab', { name: 'Review' }))

    expect(within(screen.getByRole('list', { name: 'Launch checks' })).getByText('Needs attention before launch.')).toBeInTheDocument()
    expect(screen.queryByText('Select an application before external execution')).not.toBeInTheDocument()
    expect(screen.getByText('Select a target environment before external execution')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Launch test' })).toBeDisabled()
    await user.click(screen.getByRole('tab', { name: 'Config' }))
    expect(screen.getByText('Reporting & Analysis tools')).toBeInTheDocument()
    rendered.unmount()
  })

  it('gates segmented control reveals the 7x2 custom matrix (checked = gated)', async () => {
    assistantsSearch.mockResolvedValue([])
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new?step=config')

    expect(screen.queryByTestId('gates-matrix')).not.toBeInTheDocument()
    await user.click(await screen.findByRole('radio', { name: 'Custom' }))

    const matrix = screen.getByTestId('gates-matrix')
    const checkboxes = within(matrix).getAllByRole('checkbox')
    expect(checkboxes).toHaveLength(14)
    for (const box of checkboxes) expect(box).toBeChecked() // seeded all-gated

    await user.click(within(matrix).getByLabelText('Execution prompt review gated'))
    expect(within(matrix).getByLabelText('Execution prompt review gated')).not.toBeChecked()
    expect(within(matrix).getByLabelText('Execution output review gated')).toBeChecked()
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await screen.findByText('Draft saved')
    rendered.unmount()
  })

  it('applies an organization preset from the preset bar and names it there', async () => {
    assistantsSearch.mockResolvedValue([GOLDEN, DEFAULTED, SYSTEM_DEFAULT])
    assistantsGet.mockImplementation((assistantId: string) => Promise.resolve(
      [GOLDEN, DEFAULTED, SYSTEM_DEFAULT].find(candidate => candidate.assistant_id === assistantId)))
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new?step=config')

    await applyFromBar(user, 'Use a preset…', 'Nightly checkout soak')
    // The dev server's system-created default assistant is never offered.
    expect(screen.getByRole('radio', { name: /LoadRunner/ })).toHaveAttribute('aria-checked', 'true')
    const matrix = screen.getByTestId('gates-matrix')
    expect(within(matrix).getByLabelText('Execution prompt review gated')).not.toBeChecked()
    expect(within(matrix).getByLabelText('Story Analysis prompt review gated')).toBeChecked()
    const bar = screen.getByTestId('preset-bar')
    expect(bar).toHaveTextContent('Nightly checkout soak')
    expect(bar).toHaveTextContent('no changes')

    // Switching to a preset that omits engine/gates restores the defaults instead
    // of leaking the previous preset's pinned values.
    await applyFromBar(user, 'Change preset…', 'Baseline defaults')
    expect(screen.getByRole('radio', { name: /Simulated/ })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('radio', { name: 'Review every phase' })).toBeChecked()
    expect(screen.queryByTestId('gates-matrix')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await screen.findByText('Draft saved')
    rendered.unmount()
  })

  it('shows what a preset would replace, and Back leaves the test unchanged', async () => {
    assistantsSearch.mockResolvedValue([GOLDEN])
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard('/runs/new?step=config')
    await user.click(screen.getByRole('radio', { name: 'Run automatically' }))
    await user.click(screen.getByRole('button', { name: 'Use a preset…' }))
    await user.click(await screen.findByRole('button', { name: 'Choose: Nightly checkout soak' }))
    const confirm = await screen.findByRole('dialog', { name: 'Apply Nightly checkout soak' })
    const changes = within(confirm).getByRole('table', { name: 'Settings that change' })
    expect(within(changes).getByRole('row', { name: /Engine Simulated LoadRunner/ })).toBeInTheDocument()
    expect(within(changes).getByRole('row', { name: /Review policy Run automatically Custom reviews/ })).toBeInTheDocument()
    expect(within(confirm).getByRole('button', { name: 'Back' })).toHaveFocus()
    await user.click(within(confirm).getByRole('button', { name: 'Back' }))
    await user.click(within(await screen.findByRole('dialog', { name: 'Choose a preset' })).getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('radio', { name: 'Run automatically' })).toBeChecked()
    expect(screen.getByTestId('preset-bar')).toHaveTextContent('Not started from a preset')
    rendered.unmount()
  })

  it('clears the previous traffic target when a preset explicitly has no traffic entry', async () => {
    const topology = {
      schema_version: 1,
      participants: [{ id: 'preset-backend', layer: 'backend', project_id: 'demo', application_id: 'app-checkout', environment_id: 'env-staging' }],
      primary_participant_id: 'preset-backend', traffic_entry_participant_id: null,
    }
    assistantsSearch.mockResolvedValue([{ ...GOLDEN, config: { configurable: { engine: 'sim', test_scope: topology } } }])
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard()
    await fillScope(user, screen)
    await applyFromBar(user, 'Use a preset…', 'Nightly checkout soak')
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    const preview = JSON.parse(screen.getByTestId('launch-payload-json').textContent!)
    expect(preview.configurable.test_scope).toEqual(topology)
    expect(preview.configurable.environment_id).toBeUndefined()
    rendered.unmount()
  })

  it('requires an explicit supported engine when a preset pins an unknown engine', async () => {
    assistantsSearch.mockResolvedValue([{ ...GOLDEN, config: { configurable: { engine: 'apexload' } } }])
    installWizardHandlers()
    const user = userEvent.setup()
    const rendered = renderWizard()
    await fillScope(user, screen)
    await user.click(screen.getByRole('tab', { name: 'Config' }))
    await applyFromBar(user, 'Use a preset…', 'Nightly checkout soak')
    expect(screen.getByText(/pins engine “apexload”/)).toBeInTheDocument()
    const engines = within(screen.getByRole('radiogroup', { name: 'Execution engine' })).getAllByRole('radio')
    expect(engines.every(engine => engine.getAttribute('aria-checked') === 'false')).toBe(true)
    expect(engines.every(engine => !engine.classList.contains('wizard-engine-card--selected'))).toBe(true)
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    expect(screen.getByText('apexload (unsupported)')).toBeInTheDocument()
    expect(screen.getByText(/Choose a supported engine before execution readiness can be determined/)).toBeInTheDocument()
    expect(screen.queryByText(/Simulated execution does not require/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Launch test' })).toBeDisabled()
    await user.click(screen.getByRole('tab', { name: 'Config' }))
    await user.click(screen.getByRole('radio', { name: /Simulated/ }))
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    expect(screen.getByRole('button', { name: 'Launch test' })).toBeEnabled()
    rendered.unmount()
  })

  it('applies a deep-linked preset (old ?golden= links too) and drops the parameter', async () => {
    assistantsSearch.mockResolvedValue([GOLDEN, DEFAULTED])
    installWizardHandlers()
    const rendered = renderWizard('/runs/new?step=config&golden=asst-gold')

    await waitFor(() =>
      expect(screen.getByRole('radio', { name: /LoadRunner/ })).toHaveAttribute('aria-checked', 'true'))
    await waitFor(() => expect(rendered.router.state.location.search).not.toContain('golden='))

    await act(async () => {
      await rendered.router.navigate('/runs/new?step=config&preset=organization:asst-defaulted')
    })
    await waitFor(() =>
      expect(screen.getByRole('radio', { name: /Simulated/ })).toHaveAttribute('aria-checked', 'true'))
    expect(screen.getByRole('radio', { name: 'Review every phase' })).toBeChecked()
    expect(screen.getByTestId('preset-bar')).toHaveTextContent('Baseline defaults')
    await waitFor(() => expect(rendered.router.state.location.search).not.toContain('preset='))
    rendered.unmount()
  })
})


it.each([
  { prompt_id: 'system-alternative', version_id: 'alternative-v1' },
  { prompt_id: 'system-default', version_id: 'system-default-v1', override_content: 'Keep these customized instructions' },
])('requires confirmation before replacing selected prompt choices: $prompt_id', async selection => {
  assistantsSearch.mockResolvedValue([GOLDEN])
  const draft = emptyDraft()
  draft.prompt_selections!.system = selection
  installWizardHandlers([draftRead({ id: 'selected-prompts', payload: draft as unknown as Record<string, unknown> })])
  const user = userEvent.setup()
  const rendered = renderWizard('/runs/new?draft=selected-prompts&step=config')
  await user.click(await screen.findByRole('button', { name: 'Use a preset…' }))
  await user.click(await screen.findByRole('button', { name: 'Choose: Nightly checkout soak' }))
  const dialog = await screen.findByRole('dialog', { name: 'Apply Nightly checkout soak' })
  // The confirmation lists the prompt selections it would replace.
  expect(within(dialog).getByRole('row', { name: /^Prompts / })).toBeInTheDocument()
  await user.click(within(dialog).getByRole('button', { name: 'Back' }))
  expect(screen.getByTestId('preset-bar')).toHaveTextContent('Not started from a preset')
  rendered.unmount()
})
