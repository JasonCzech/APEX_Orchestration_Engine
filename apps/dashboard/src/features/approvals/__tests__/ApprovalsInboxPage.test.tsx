/**
 * Approvals inbox (plan UX 2.b): queue derivation, deep links, the keyboard
 * layer, auto-advance, conflict rows and the sidebar badge.
 *
 * GateModule is replaced by a typed module mock (./gateModuleMock) — the
 * machine's own behavior is covered by the gate agent's src/hitl tests; here
 * the mock drives the inbox's outcome handling deterministically.
 */
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { queryKeys } from '@/api/queryKeys'
import { RUN_BUSY } from '@/features/runs/runsTestHandlers'
import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import {
  detailsHandler,
  gatedDetail,
  gatedRun,
  mutableListHandler,
} from './approvalsTestHandlers'
import { invokedActions, resetGateModuleMock } from './gateModuleMock'

// Partial mock: only the self-contained GateModule is replaced — other
// surfaces in the test route tree (run detail's GateModuleView) stay real.
vi.mock('@/hitl/GateModule', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/hitl/GateModule')>()),
  GateModule: (await import('./gateModuleMock')).MockGateModule,
}))

// Dynamic timestamps keep age/stale math independent of the wall clock.
const T_OLDEST = new Date(Date.now() - 60 * 60_000).toISOString() // 1h ago (stale)
const T_NEWER = new Date(Date.now() - 5 * 60_000).toISOString() // 5m ago

const OLD = gatedRun('run-old-1', {
  kind: 'prompt_review',
  phase: 'test_planning',
  interruptId: 'int-old',
  updatedAt: T_OLDEST,
  title: 'Oldest gated run',
})
const NEW = gatedRun('run-new-2', {
  kind: 'phase_review',
  phase: 'reporting',
  interruptId: 'int-new',
  updatedAt: T_NEWER,
  title: 'Newest gated run',
})

const DETAILS = {
  [OLD.thread_id]: gatedDetail(OLD),
  [NEW.thread_id]: gatedDetail(NEW),
}

function useInboxHandlers(items = [OLD, RUN_BUSY, NEW]) {
  const { handler, source } = mutableListHandler(items)
  server.use(handler, detailsHandler(DETAILS))
  return source
}

function renderInbox(initialEntry = '/approvals') {
  return renderApp({ initialEntries: [initialEntry], authState: authenticatedState() })
}

async function findRow(threadId: string) {
  return await screen.findByTestId(`approvals-row-${threadId}`)
}

beforeEach(() => {
  resetGateModuleMock()
})

describe('ApprovalsInboxPage', () => {
  it('lists only pending gates, oldest-updated first, with kind/phase/project metadata', async () => {
    useInboxHandlers()
    renderInbox()

    const queue = await screen.findByRole('listbox', { name: 'Approvals queue' })
    const rows = within(queue).getAllByRole('option')
    expect(rows).toHaveLength(2) // RUN_BUSY (no pending gate) filtered out
    expect(rows[0]).toHaveAttribute('id', 'approvals-row-run-old-1')
    expect(rows[1]).toHaveAttribute('id', 'approvals-row-run-new-2')

    const oldRow = within(rows[0] as HTMLElement)
    expect(oldRow.getByText('prompt_review')).toHaveClass('topbar-meta-chip', 'accent')
    expect(oldRow.getByText('test_planning')).toBeInTheDocument()
    expect(oldRow.getByText('proj-alpha')).toBeInTheDocument()
    // > 15m old -> amber pulsing age
    expect(oldRow.getByText(/ago$/)).toHaveClass('approvals-age', 'stale')
    expect(within(rows[1] as HTMLElement).getByText('phase_review')).toHaveClass(
      'topbar-meta-chip',
      'info',
    )
    expect(screen.getByTestId('approvals-count-chip')).toHaveTextContent('2 pending')
  })

  it('auto-selects the oldest gate and previews it through the shared GateModule', async () => {
    useInboxHandlers()
    renderInbox()

    expect(await findRow('run-old-1')).toHaveAttribute('aria-selected', 'true')
    const gate = await screen.findByTestId('gate-module-mock')
    expect(gate).toHaveAttribute('data-thread', 'run-old-1')
    expect(gate).toHaveAttribute('data-interrupt', 'int-old')
    expect(gate).toHaveAttribute('data-compact', 'true')
  })

  it('deep link /approvals/:threadId/:interruptId selects that queue item', async () => {
    useInboxHandlers()
    renderInbox('/approvals/run-new-2/int-new')

    expect(await findRow('run-new-2')).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByTestId('gate-module-mock')).toHaveAttribute(
      'data-interrupt',
      'int-new',
    )
    expect(screen.getByTestId('approvals-row-run-old-1')).toHaveAttribute(
      'aria-selected',
      'false',
    )
  })

  it('navigates the queue with j/k', async () => {
    useInboxHandlers()
    const user = userEvent.setup()
    renderInbox()
    expect(await findRow('run-old-1')).toHaveAttribute('aria-selected', 'true')

    await user.keyboard('j')
    expect(await findRow('run-new-2')).toHaveAttribute('aria-selected', 'true')
    await user.keyboard('j') // clamped at the end
    expect(await findRow('run-new-2')).toHaveAttribute('aria-selected', 'true')
    await user.keyboard('k')
    expect(await findRow('run-old-1')).toHaveAttribute('aria-selected', 'true')
  })

  it('o opens the selected run', async () => {
    useInboxHandlers()
    // Quiet the run-detail page's live-run discovery once it mounts.
    server.use(http.get('*/threads/:threadId/runs', () => HttpResponse.json([])))
    const user = userEvent.setup()
    const { router } = renderInbox()
    // Wait for SELECTION, not mere row presence: auto-select commits in an
    // effect after the row renders, and 'o' reads the selection — a keystroke
    // fired in that gap is silently dropped (one-shot, no retry possible).
    await waitFor(() =>
      expect(screen.getByTestId('approvals-row-run-old-1')).toHaveAttribute(
        'aria-selected',
        'true',
      ),
    )

    await user.keyboard('o')
    await waitFor(() =>
      expect(router.state.location.pathname).toMatch(/^\/runs\/run-old-1/),
    )
  })

  it('disables shortcuts while typing in an input', async () => {
    useInboxHandlers()
    const user = userEvent.setup()
    renderInbox()
    expect(await findRow('run-old-1')).toHaveAttribute('aria-selected', 'true')
    await screen.findByTestId('gate-module-mock')

    await user.click(screen.getByLabelText('mock-note'))
    await user.keyboard('ja') // would navigate + approve outside an input
    expect(screen.getByTestId('approvals-row-run-old-1')).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(invokedActions).toHaveLength(0)
    expect(screen.getByLabelText('mock-note')).toHaveValue('ja')
  })

  it('a approves via the gate handle, grays the row and auto-advances', async () => {
    useInboxHandlers()
    const user = userEvent.setup()
    renderInbox()
    expect(await findRow('run-old-1')).toHaveAttribute('aria-selected', 'true')
    await screen.findByTestId('gate-module-mock')

    await user.keyboard('a')
    expect(invokedActions).toEqual(['approve'])

    // Row grayed inline ('actioned', not removed) + selection advanced.
    const oldRow = await findRow('run-old-1')
    expect(oldRow).toHaveClass('removed')
    expect(within(oldRow).getByText('actioned')).toBeInTheDocument()
    expect(await findRow('run-new-2')).toHaveAttribute('aria-selected', 'true')
    await waitFor(() =>
      expect(screen.getByTestId('gate-module-mock')).toHaveAttribute(
        'data-thread',
        'run-new-2',
      ),
    )
  })

  it('a superseded outcome grays the row as actioned elsewhere and advances', async () => {
    useInboxHandlers()
    const user = userEvent.setup()
    renderInbox()
    expect(await findRow('run-old-1')).toHaveAttribute('aria-selected', 'true')

    await user.click(await screen.findByRole('button', { name: 'mock-supersede' }))

    const oldRow = await findRow('run-old-1')
    expect(oldRow).toHaveClass('removed')
    expect(within(oldRow).getByText('actioned elsewhere')).toBeInTheDocument()
    expect(await findRow('run-new-2')).toHaveAttribute('aria-selected', 'true')
  })

  it('keeps a row that vanished between polls grayed inline until the next poll', async () => {
    const source = useInboxHandlers()
    const { queryClient } = renderInbox()
    await findRow('run-old-1')

    // The gate was resumed from another surface: the next poll drops the row.
    source.current = [NEW, RUN_BUSY]
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.pipelines.lists() })
    })

    const oldRow = await findRow('run-old-1')
    expect(oldRow).toHaveClass('removed')
    expect(oldRow).toHaveAttribute('aria-disabled', 'true')
    expect(within(oldRow).getByText('actioned elsewhere')).toBeInTheDocument()
    expect(await findRow('run-new-2')).not.toHaveClass('removed')
  })

  it('? toggles the shortcuts overlay and Escape closes it', async () => {
    useInboxHandlers()
    const user = userEvent.setup()
    renderInbox()
    await findRow('run-old-1')

    await user.keyboard('?')
    const dialog = await screen.findByRole('dialog', { name: 'Keyboard shortcuts' })
    expect(within(dialog).getByText('Next gate')).toBeInTheDocument()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog', { name: 'Keyboard shortcuts' })).not.toBeInTheDocument()
  })

  it('shows the all-clear empty state with a ghost link to /runs', async () => {
    useInboxHandlers([RUN_BUSY]) // fleet alive, nothing gated
    renderInbox()

    const empty = await screen.findByTestId('approvals-empty')
    expect(empty).toHaveTextContent('All clear. No gates awaiting review.')
    expect(within(empty).getByRole('link', { name: 'Go to runs' })).toHaveAttribute(
      'href',
      '/runs',
    )
  })
})

describe('Sidebar approvals badge', () => {
  it('shows the pending-gate count next to Approvals and pulses when a gate is stale', async () => {
    useInboxHandlers()
    renderApp({ initialEntries: ['/'], authState: authenticatedState() })

    const badge = await screen.findByTestId('approvals-nav-badge')
    expect(badge).toHaveTextContent('2')
    expect(badge).toHaveClass('dash-badge', 'pulse') // OLD waited > 15m
    expect(screen.getByRole('link', { name: /Approvals/ })).toContainElement(badge)
  })

  it('renders no badge when nothing is pending (and no pulse when gates are fresh)', async () => {
    // Default server handler: empty pipelines list.
    renderApp({ initialEntries: ['/'], authState: authenticatedState() })
    await screen.findByTestId('sidebar')
    await waitFor(() =>
      expect(screen.queryByTestId('approvals-nav-badge')).not.toBeInTheDocument(),
    )
  })
})
