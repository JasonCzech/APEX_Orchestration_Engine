/**
 * Config step: phase-subset toggles with warn-only dependency hints, the
 * gates segmented control + custom matrix, and the golden-config picker
 * pre-filling engine/gates from the assistant's configurable.
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { installWizardHandlers, renderWizard } from './wizardTestUtils'

const { assistantsSearch } = vi.hoisted(() => ({ assistantsSearch: vi.fn() }))

vi.mock('@/api/langgraphClient', () => ({
  getLangGraphClient: () =>
    Promise.resolve({
      assistants: { search: assistantsSearch },
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

describe('ConfigStep', () => {
  it('toggling a phase off shows the warn-only dependency hint and keeps Next enabled', async () => {
    assistantsSearch.mockResolvedValue([])
    installWizardHandlers()
    const user = userEvent.setup()
    renderWizard('/runs/new?step=config')

    const strip = await screen.findByRole('group', { name: 'Phase subset' })
    const toggles = within(strip).getAllByRole('button')
    expect(toggles).toHaveLength(7)
    for (const toggle of toggles) expect(toggle).toHaveAttribute('aria-pressed', 'true')

    // Drop execution: reporting's prereq is no longer earlier in the plan.
    await user.click(within(strip).getByRole('button', { name: 'execution' }))
    const hints = await screen.findByTestId('phase-dependency-hints')
    expect(hints).toHaveTextContent(
      'reporting needs execution earlier in plan or succeeded on thread',
    )
    // Warn, never block: the step stays valid.
    expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled()

    // Toggling execution back on clears the hint (all-7 = null subset again).
    await user.click(within(strip).getByRole('button', { name: 'execution' }))
    await waitFor(() =>
      expect(screen.queryByTestId('phase-dependency-hints')).not.toBeInTheDocument(),
    )
  })

  it('gates segmented control reveals the 7x2 custom matrix (checked = gated)', async () => {
    assistantsSearch.mockResolvedValue([])
    installWizardHandlers()
    const user = userEvent.setup()
    renderWizard('/runs/new?step=config')

    expect(screen.queryByTestId('gates-matrix')).not.toBeInTheDocument()
    await user.click(await screen.findByRole('button', { name: 'Custom' }))

    const matrix = screen.getByTestId('gates-matrix')
    const checkboxes = within(matrix).getAllByRole('checkbox')
    expect(checkboxes).toHaveLength(14)
    for (const box of checkboxes) expect(box).toBeChecked() // seeded all-gated

    await user.click(within(matrix).getByLabelText('execution prompt review gated'))
    expect(within(matrix).getByLabelText('execution prompt review gated')).not.toBeChecked()
    expect(within(matrix).getByLabelText('execution output review gated')).toBeChecked()
  })

  it('golden-config pick shows the inherited chip and pre-fills engine + gates', async () => {
    assistantsSearch.mockResolvedValue([GOLDEN, SYSTEM_DEFAULT])
    installWizardHandlers()
    const user = userEvent.setup()
    renderWizard('/runs/new?step=config')

    // The dev server's system-created default assistant is filtered out.
    expect(await screen.findByRole('button', { name: /Nightly checkout soak/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^pipeline$/ })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /Nightly checkout soak/ }))
    expect(screen.getByTestId('config-inherited-chip')).toHaveTextContent('config inherited')

    // Engine pre-filled from the assistant's configurable…
    expect(screen.getByRole('radio', { name: /LoadRunner/ })).toHaveAttribute(
      'aria-checked',
      'true',
    )
    // …and gates landed as a custom matrix honoring the pinned auto pair.
    const matrix = screen.getByTestId('gates-matrix')
    expect(within(matrix).getByLabelText('execution prompt review gated')).not.toBeChecked()
    expect(within(matrix).getByLabelText('story analysis prompt review gated')).toBeChecked()
  })
})
