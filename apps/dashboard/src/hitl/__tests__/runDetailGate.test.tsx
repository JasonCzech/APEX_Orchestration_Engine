/**
 * Run-detail mounting (plan 2.a integration): the GateModule pins above the
 * workspace tabs on the gate's phase, other phases get the slim banner, the
 * rail's Review link targets the gate phase, and the header abort drives the
 * same machine (type-to-confirm -> CAS resume).
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { server } from '@/test/server'

import {
  PIPELINE_DETAIL_INTERRUPTED,
  pipelineDetailHandler,
  renderRunRoutes,
  THREAD_ID,
} from '@/features/runs/__tests__/testUtils'

import { resumeHandler } from './gateFixtures'

vi.mock('@uiw/react-codemirror', async () => {
  const { createElement } = await import('react')
  return {
    default: ({ value }: { value: string }) =>
      createElement('pre', { 'data-testid': 'codemirror' }, value),
  }
})

// Snapshot-only liveness (same integration-contract mock as the D1/D2 tests).
vi.mock('@/streaming/usePipelineStream', () => ({
  useRunLiveness: () => ({
    runId: null,
    stream: {
      status: 'idle',
      phaseProgress: {},
      toolCalls: [],
      engineStats: { samples: [], latest: null },
      pendingGateHint: null,
    },
  }),
}))

describe('RunDetailPage gate mounting (D3)', () => {
  it('pins the GateModule above the workspace tabs on the gate phase', async () => {
    server.use(pipelineDetailHandler(PIPELINE_DETAIL_INTERRUPTED))
    renderRunRoutes([`/runs/${THREAD_ID}/phases/reporting`])

    const workspace = await screen.findByRole('region', { name: 'Reporting workspace' })
    const module = within(workspace).getByTestId('gate-module')
    expect(module).toHaveTextContent('Phase review — Reporting')
    expect(within(module).getByTestId('gate-summary')).toHaveTextContent(
      'Draft report compiled; KPI deltas within tolerance.',
    )
    // Pinned ABOVE the tab bar.
    const tablist = within(workspace).getByRole('tablist', { name: 'Phase workspace tabs' })
    expect(module.compareDocumentPosition(tablist) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByTestId('gate-slim-banner')).not.toBeInTheDocument()
  })

  it('shows the slim banner linking to the gate phase from other phases', async () => {
    server.use(pipelineDetailHandler(PIPELINE_DETAIL_INTERRUPTED))
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution?tab=output`])

    const banner = await screen.findByTestId('gate-slim-banner')
    expect(banner).toHaveTextContent('Phase review gate open on Reporting')
    expect(within(banner).getByRole('link', { name: 'Review' })).toHaveAttribute(
      'href',
      `/runs/${THREAD_ID}/phases/reporting`,
    )
    expect(screen.queryByTestId('gate-module')).not.toBeInTheDocument()
  })

  it('header abort uses the same machine: type-to-confirm then CAS resume', async () => {
    server.use(pipelineDetailHandler(PIPELINE_DETAIL_INTERRUPTED))
    const resume = resumeHandler(202)
    server.use(resume.handler)
    const user = userEvent.setup()
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution?tab=output`])

    // Header abort renders while the gate machine is open (banner route has
    // exactly this one Abort button — the full module lives on /reporting).
    const abort = await screen.findByRole('button', { name: 'Abort' })
    await user.click(abort)
    const confirm = screen.getByRole('button', { name: 'Confirm abort' })
    expect(confirm).toBeDisabled()
    await user.type(screen.getByLabelText('Type ABORT to confirm'), 'ABORT')
    await user.click(confirm)

    await waitFor(() =>
      expect(resume.captured.last()).toMatchObject({
        threadId: THREAD_ID,
        interruptId: 'int-1',
        body: { action: 'abort' },
      }),
    )
  })
})
