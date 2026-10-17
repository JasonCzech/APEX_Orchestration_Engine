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
import {
  invokedActions,
  invokedActionThreads,
  resetGateModuleMock,
} from './gateModuleMock'

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
  // The exact thread-detail paths are fetched eagerly as the queue auto-selects
  // the oldest gate; register both the param matcher and exact paths so the
  // full-suite run doesn't depend on matcher timing.
  server.use(
    handler,
    detailsHandler(DETAILS),
    http.get('*/v1/pipelines/run-old-1', () => HttpResponse.json(DETAILS['run-old-1'])),
    http.get('*/v1/pipelines/run-new-2', () => HttpResponse.json(DETAILS['run-new-2'])),
  )
  return source
}

function renderInbox(initialEntry = '/approvals') {
  return renderApp({ initialEntries: [initialEntry], authState: authenticatedState() })
}

async function findRow(threadId: string) {
  return await screen.findByTestId(`approvals-row-${threadId}`)
}

async function waitForSelected(threadId: string) {
  await waitFor(() =>
    expect(screen.getByTestId(`approvals-row-${threadId}`)).toHaveAttribute(
      'aria-selected',
      'true',
    ),
  )
}

beforeEach(() => {
  resetGateModuleMock()
})

describe('ApprovalsInboxPage', () => {
  it('lists only pending gates, oldest-updated first, with kind/phase/project metadata', async () => {
    useInboxHandlers()
    renderInbox()

    const queue = await screen.findByRole('listbox', { name: 'Needs review queue' })
    const rows = within(queue).getAllByRole('option')
    expect(rows).toHaveLength(2) // RUN_BUSY (no pending gate) filtered out
    expect(rows[0]).toHaveAttribute('id', 'approvals-row-run-old-1')
    expect(rows[1]).toHaveAttribute('id', 'approvals-row-run-new-2')

    const oldRow = within(rows[0] as HTMLElement)
    expect(oldRow.getByText('Review prompts')).toHaveClass('topbar-meta-chip', 'accent')
    expect(oldRow.getByText('Test Planning')).toBeInTheDocument()
    expect(oldRow.getByText('proj-alpha')).toBeInTheDocument()
    // > 15m old -> amber pulsing age
    expect(oldRow.getByText(/ago$/)).toHaveClass('approvals-age', 'stale')
    expect(within(rows[1] as HTMLElement).getByText('Review phase results')).toHaveClass(
      'topbar-meta-chip',
      'info',
    )
    expect(screen.queryByText('Test operations')).not.toBeInTheDocument()
    expect(screen.queryByText('2 pending')).not.toBeInTheDocument()
  })

  it('walks every backend page so queues larger than 100 stay complete', async () => {
    const runs = Array.from({ length: 101 }, (_, index) =>
      gatedRun(`run-page-${String(index).padStart(3, '0')}`, {
        kind: 'prompt_review',
        phase: 'test_planning',
        interruptId: `int-page-${index}`,
        updatedAt: new Date(Date.now() - (101 - index) * 60_000).toISOString(),
      }),
    )
    const offsets: number[] = []
    server.use(
      http.get('*/v1/pipelines', ({ request }) => {
        const params = new URL(request.url).searchParams
        const offset = Number(params.get('offset') ?? '0')
        const limit = Number(params.get('limit') ?? '100')
        offsets.push(offset)
        return HttpResponse.json({ items: runs.slice(offset, offset + limit), limit, offset })
      }),
      http.get('*/v1/pipelines/:threadId', ({ params }) => {
        const row = runs.find((candidate) => candidate.thread_id === params.threadId)
        return row
          ? HttpResponse.json(gatedDetail(row))
          : HttpResponse.json({ detail: 'unknown thread' }, { status: 404 })
      }),
    )
    renderInbox()

    expect(await findRow('run-page-100')).toBeInTheDocument()
    expect(offsets).toEqual([0, 100])
  })

  it('auto-selects the oldest gate and previews it through the shared GateModule', async () => {
    useInboxHandlers()
    const { router } = renderInbox()

    await waitFor(() => expect(screen.getByTestId('approvals-row-run-old-1')).toHaveAttribute('aria-selected', 'true'))
    const gate = await screen.findByTestId('gate-module-mock')
    expect(gate).toHaveAttribute('data-thread', 'run-old-1')
    expect(gate).toHaveAttribute('data-interrupt', 'int-old')
    expect(gate).toHaveAttribute('data-compact', 'false')
    expect(router.state.location.pathname).toBe('/runs/run-old-1/reviews/int-old')
    expect(router.state.location.search).toBe('?source=queue')
    expect(screen.getByRole('region', { name: 'Run workspace' })).toHaveTextContent('Test result:')
    expect(screen.queryByRole('button', { name: 'Re-run selected plan' })).not.toBeInTheDocument()
  })

  it('preserves entry provenance while moving through Review Workspace sections', async () => {
    useInboxHandlers()
    const user = userEvent.setup()
    const { router } = renderInbox('/runs/run-old-1/reviews/int-old?source=history')

    await user.click(await screen.findByRole('button', { name: 'Overview' }))
    expect(router.state.location.pathname).toBe('/runs/run-old-1/reviews/int-old')
    expect(router.state.location.search).toBe('?source=history&section=overview')
  })

  it('deep link /approvals/:threadId/:interruptId selects that queue item', async () => {
    useInboxHandlers()
    const { router } = renderInbox('/approvals/run-new-2/int-new')

    expect(await findRow('run-new-2')).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByTestId('gate-module-mock')).toHaveAttribute(
      'data-interrupt',
      'int-new',
    )
    expect(screen.getByTestId('approvals-row-run-old-1')).toHaveAttribute(
      'aria-selected',
      'false',
    )
    await waitFor(() => expect(router.state.location.pathname).toBe('/runs/run-new-2/reviews/int-new'))
  })

  it.each([false, true])('preserves a canonical review missing from the queue snapshot (empty queue: %s)', async empty => {
    const source = useInboxHandlers(empty ? [] : [OLD])
    const { router, queryClient } = renderInbox('/runs/run-new-2/reviews/int-new?source=overview')
    const gate = await screen.findByTestId('gate-module-mock')
    expect(gate).toHaveAttribute('data-thread', NEW.thread_id)
    expect(gate).toHaveAttribute('data-interrupt', 'int-new')
    expect(router.state.location.pathname).toBe('/runs/run-new-2/reviews/int-new')
    expect(screen.queryByTestId('approvals-empty')).not.toBeInTheDocument()
    expect(screen.getByText('Selected review is outside the current queue snapshot')).toBeInTheDocument()

    source.current = [OLD, NEW]
    await act(async () => { await queryClient.invalidateQueries({ queryKey: queryKeys.approvals.inbox() }) })
    await waitForSelected(NEW.thread_id)
    expect(router.state.location.pathname).toBe('/runs/run-new-2/reviews/int-new')
    expect(screen.queryByText('Selected review is outside the current queue snapshot')).not.toBeInTheDocument()
  })

  it('preserves a new interrupt link when the queue still carries the prior interrupt for that run', async () => {
    useInboxHandlers([OLD, { ...NEW, pending_gate: { ...NEW.pending_gate!, interrupt_id: 'int-previous' } }])
    const { router } = renderInbox('/runs/run-new-2/reviews/int-new?source=overview')
    expect(await screen.findByTestId('gate-module-mock')).toHaveAttribute('data-interrupt', 'int-new')
    expect(router.state.location.pathname).toBe('/runs/run-new-2/reviews/int-new')
    expect(screen.getByText('Selected review is outside the current queue snapshot')).toBeInTheDocument()
  })

  it('loads the requested review from its own snapshot when the inbox fails', async () => {
    useInboxHandlers()
    server.use(http.get('*/v1/pipelines', () => HttpResponse.json({ detail: 'Queue temporarily unavailable' }, { status: 503 })))
    const { router } = renderInbox('/runs/run-new-2/reviews/int-new?source=overview')
    expect(await screen.findByTestId('gate-module-mock')).toHaveAttribute('data-interrupt', 'int-new')
    expect(await screen.findByText('Queue temporarily unavailable')).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/runs/run-new-2/reviews/int-new')
    expect(screen.getByRole('region', { name: 'Pending reviews' })).toContainElement(screen.getByRole('button', { name: 'Retry' }))
  })

  it('protects an unlisted local revision through a stale queue echo and clears it after settling', async () => {
    const source = useInboxHandlers([OLD])
    const user = userEvent.setup()
    const { router, queryClient } = renderInbox('/runs/run-new-2/reviews/int-new?source=overview')
    await user.click(await screen.findByRole('button', { name: 'mock-revise' }))
    source.current = [OLD, NEW]
    await act(async () => { await queryClient.invalidateQueries({ queryKey: queryKeys.approvals.inbox() }) })
    await waitForSelected(NEW.thread_id)
    source.current = [OLD]
    await act(async () => { await queryClient.invalidateQueries({ queryKey: queryKeys.approvals.inbox() }) })
    expect(router.state.location.pathname).toBe('/runs/run-new-2/reviews/int-new')
    expect(screen.getByTestId('gate-module-mock')).toHaveAttribute('data-thread', NEW.thread_id)
    server.use(http.get('*/v1/pipelines/run-new-2', () => HttpResponse.json({ ...DETAILS[NEW.thread_id], thread_status: 'idle', pending_gate: null, interrupts: [] })))
    await act(async () => { await queryClient.invalidateQueries({ queryKey: queryKeys.threads.state(NEW.thread_id) }) })
    await waitFor(() => expect(router.state.location.pathname).toBe('/runs/run-old-1/reviews/int-old'))
  })

  it('keeps the run’s reopened review when the refreshed inbox still omits it', async () => {
    const source = useInboxHandlers([OLD])
    const user = userEvent.setup()
    const { router, queryClient } = renderInbox('/runs/run-new-2/reviews/int-new?source=overview')
    await user.click(await screen.findByRole('button', { name: 'mock-revise' }))
    source.current = [OLD, NEW]
    await act(async () => { await queryClient.invalidateQueries({ queryKey: queryKeys.approvals.inbox() }) })
    await waitForSelected(NEW.thread_id)
    source.current = [OLD]
    await act(async () => { await queryClient.invalidateQueries({ queryKey: queryKeys.approvals.inbox() }) })
    const reopened = { ...NEW, pending_gate: { ...NEW.pending_gate!, interrupt_id: 'int-revised' }, updated_at: new Date(Date.parse(T_NEWER) + 1000).toISOString() }
    server.use(http.get('*/v1/pipelines/run-new-2', () => HttpResponse.json(gatedDetail(reopened))))
    await act(async () => { await queryClient.invalidateQueries({ queryKey: queryKeys.threads.state(NEW.thread_id) }) })
    await waitFor(() => expect(screen.getByTestId('gate-module-mock')).toHaveAttribute('data-interrupt', 'int-revised'))
    await user.click(screen.getByRole('button', { name: 'mock-reopened' }))
    await act(async () => { await queryClient.invalidateQueries({ queryKey: queryKeys.approvals.inbox() }) })
    expect(router.state.location.pathname).toBe('/runs/run-new-2/reviews/int-revised')
    expect(screen.getByTestId('gate-module-mock')).toHaveAttribute('data-thread', NEW.thread_id)
    expect(screen.getByTestId('gate-module-mock')).toHaveAttribute('data-interrupt', 'int-revised')
    source.current = [OLD, reopened]
    await act(async () => { await queryClient.invalidateQueries({ queryKey: queryKeys.approvals.inbox() }) })
    await waitForSelected(NEW.thread_id)
    expect(screen.queryByText('Selected review is outside the current queue snapshot')).not.toBeInTheDocument()
  })

  it('routes a same-turn safe focus shortcut only to the new deep-linked gate', async () => {
    useInboxHandlers()
    const { router } = renderInbox('/approvals/run-old-1/int-old')

    expect(await screen.findByTestId('gate-module-mock')).toHaveAttribute(
      'data-thread',
      'run-old-1',
    )
    await act(async () => router.navigate('/approvals/run-new-2/int-new'))
    await waitFor(() =>
      expect(screen.getByTestId('gate-module-mock')).toHaveAttribute(
        'data-thread',
        'run-new-2',
      ),
    )
    await act(async () => router.navigate('/approvals/run-old-1/int-old'))
    await waitFor(() =>
      expect(screen.getByTestId('gate-module-mock')).toHaveAttribute(
        'data-thread',
        'run-old-1',
      ),
    )
    resetGateModuleMock()

    // Both gate snapshots are cached. Dispatch before passive effects run so
    // this covers the route-commit window that previously retained the old
    // selection and imperative handle.
    act(() => {
      void router.navigate('/approvals/run-new-2/int-new', { flushSync: true })
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'm', bubbles: true }))
    })

    expect(invokedActions).toEqual(['modify'])
    expect(invokedActionThreads).toEqual(['run-new-2'])
    expect(await findRow('run-new-2')).not.toHaveClass('removed')
    expect(await findRow('run-old-1')).not.toHaveClass('removed')
  })

  it('navigates the queue with j/k', async () => {
    useInboxHandlers()
    const user = userEvent.setup()
    renderInbox()
    await waitFor(() =>
      expect(screen.getByTestId('approvals-row-run-old-1')).toHaveAttribute(
        'aria-selected',
        'true',
      ),
    )

    await user.keyboard('j')
    expect(await findRow('run-new-2')).toHaveFocus()
    expect(await findRow('run-new-2')).toHaveAttribute('aria-posinset', '2')
    expect(await findRow('run-new-2')).toHaveAttribute('aria-selected', 'true')
    const updates = await screen.findByRole('status', { name: 'Run updates' })
    await waitFor(() => expect(updates).toHaveTextContent('Newest gated run. Review phase results open on Reporting. Queue item 2 of 2.'))
    expect(updates).not.toHaveTextContent('Snapshot refresh active')
    expect(updates).not.toHaveTextContent('Story Analysis:')
    await user.keyboard('j') // clamped at the end
    expect(await findRow('run-new-2')).toHaveAttribute('aria-selected', 'true')
    await user.keyboard('k')
    expect(await findRow('run-old-1')).toHaveFocus()
    expect(await findRow('run-old-1')).toHaveAttribute('aria-selected', 'true')
    await waitFor(() => expect(screen.getByRole('status', { name: 'Run updates' })).toHaveTextContent('Oldest gated run. Review prompts open on Test Planning. Queue item 1 of 2.'))
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
    await waitForSelected('run-old-1')
    await screen.findByTestId('gate-module-mock')

    await user.click(screen.getByLabelText('mock-note'))
    await user.keyboard('jm') // would navigate + move focus outside an input
    expect(screen.getByTestId('approvals-row-run-old-1')).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(invokedActions).toHaveLength(0)
    expect(screen.getByLabelText('mock-note')).toHaveValue('jm')
  })

  it('bare a/s letters never invoke approval mutations', async () => {
    useInboxHandlers()
    const user = userEvent.setup()
    renderInbox()
    await waitForSelected('run-old-1')
    await screen.findByTestId('gate-module-mock')

    await user.keyboard('as')
    expect(invokedActions).toHaveLength(0)
    expect(await findRow('run-old-1')).not.toHaveClass('removed')
    expect(await findRow('run-old-1')).toHaveAttribute('aria-selected', 'true')
    expect(await findRow('run-new-2')).not.toHaveClass('removed')
  })

  it('an explicit gate approval control grays the row and auto-advances', async () => {
    useInboxHandlers()
    const user = userEvent.setup()
    renderInbox()
    await waitForSelected('run-old-1')

    await user.click(await screen.findByRole('button', { name: 'mock-approve' }))
    // Row grayed inline ('actioned', not removed) + selection advanced.
    const oldRow = await findRow('run-old-1')
    expect(oldRow).toHaveClass('removed')
    expect(within(oldRow).getByText('actioned')).toBeInTheDocument()
    expect(screen.getByText('Decision receipt')).toBeInTheDocument()
    expect(screen.getByText(/Approved prompts and started the phase/)).toBeInTheDocument()
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
    await waitFor(() =>
      expect(screen.getByTestId('approvals-row-run-old-1')).toHaveAttribute(
        'aria-selected',
        'true',
      ),
    )

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

  it('retains a locally reopening gate when the list temporarily loses its interrupt', async () => {
    const source = useInboxHandlers()
    const user = userEvent.setup()
    const { queryClient, router } = renderInbox()
    await user.click(await screen.findByRole('button', { name: 'mock-revise' }))
    source.current = [NEW]
    await act(async () => { await queryClient.invalidateQueries({ queryKey: queryKeys.approvals.inbox() }) })
    expect(await findRow(OLD.thread_id)).not.toHaveClass('removed')
    expect(await findRow(OLD.thread_id)).toHaveTextContent('Agent working')
    expect(router.state.location.pathname).toContain(OLD.thread_id)
    const reopened = { ...OLD, pending_gate: { ...OLD.pending_gate!, interrupt_id: 'int-revised' } }
    source.current = [reopened, NEW]
    server.use(http.get('*/v1/pipelines/run-old-1', () => HttpResponse.json(gatedDetail(reopened))))
    // The thread finishes before the separate inbox poll catches up. The
    // completion callback must refresh the inbox before releasing the row.
    await user.click(screen.getByRole('button', { name: 'mock-reopened' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/runs/run-old-1/reviews/int-revised'))
    expect(await findRow(OLD.thread_id)).not.toHaveTextContent('actioned elsewhere')
  })

  it('keeps a local reopening row through an inbox refresh failure and recovers on retry', async () => {
    const source = useInboxHandlers()
    const user = userEvent.setup()
    const { queryClient, router } = renderInbox()
    await user.click(await screen.findByRole('button', { name: 'mock-revise' }))
    source.current = [NEW]
    await act(async () => { await queryClient.invalidateQueries({ queryKey: queryKeys.approvals.inbox() }) })
    const reopened = { ...OLD, pending_gate: { ...OLD.pending_gate!, interrupt_id: 'int-recovered' } }
    source.current = [reopened, NEW]
    server.use(http.get('*/v1/pipelines', () => new HttpResponse(null, { status: 503 }), { once: true }))
    await user.click(screen.getByRole('button', { name: 'mock-reopened' }))
    await waitFor(() => expect(queryClient.getQueryState(queryKeys.approvals.inbox())?.status).toBe('error'))
    expect(await findRow(OLD.thread_id)).toHaveTextContent('Agent working')
    expect(router.state.location.pathname).toContain(OLD.thread_id)
    await act(async () => { await queryClient.invalidateQueries({ queryKey: queryKeys.approvals.inbox() }) })
    await waitFor(() => expect(router.state.location.pathname).toBe('/runs/run-old-1/reviews/int-recovered'))
    expect(await findRow(OLD.thread_id)).not.toHaveTextContent('actioned elsewhere')
  })

  it('reconciles a local re-review after leaving its workspace, without retaining a working placeholder', async () => {
    const source = useInboxHandlers()
    const user = userEvent.setup()
    const { queryClient } = renderInbox()
    await user.click(await screen.findByRole('button', { name: 'mock-revise' }))
    await user.click(await findRow(NEW.thread_id))
    const reopened = { ...OLD, title: 'Reopened background review', pending_gate: { ...OLD.pending_gate!, interrupt_id: 'int-background' } }
    source.current = [reopened, NEW]
    await act(async () => { await queryClient.invalidateQueries({ queryKey: queryKeys.approvals.inbox() }) })
    await waitFor(() => expect(screen.getByTestId(`approvals-row-${OLD.thread_id}`)).toHaveTextContent('Reopened background review'))
    expect(await findRow(OLD.thread_id)).not.toHaveTextContent('Agent working')
    source.current = [NEW]
    await act(async () => { await queryClient.invalidateQueries({ queryKey: queryKeys.approvals.inbox() }) })
    await act(async () => { await queryClient.invalidateQueries({ queryKey: queryKeys.approvals.inbox() }) })
    await waitFor(() => {
      const priorRow = screen.queryByTestId(`approvals-row-${OLD.thread_id}`)
      if (priorRow) {
        expect(priorRow).toHaveClass('removed')
        expect(priorRow).not.toHaveTextContent('Agent working')
      }
    })
  })

  it('observes a locally revised run that finishes after its workspace is unmounted', async () => {
    const source = useInboxHandlers()
    const user = userEvent.setup()
    const { queryClient } = renderInbox()
    await user.click(await screen.findByRole('button', { name: 'mock-revise' }))
    await user.click(await findRow(NEW.thread_id))
    source.current = [NEW]
    server.use(http.get('*/v1/pipelines/run-old-1', () => HttpResponse.json({ ...DETAILS['run-old-1'], thread_status: 'error', pending_gate: null, interrupts: [] })))
    await act(async () => { await queryClient.invalidateQueries() })
    await waitFor(() => expect(screen.queryByText('Agent working — review will reopen')).not.toBeInTheDocument())
    await act(async () => { await queryClient.invalidateQueries({ queryKey: queryKeys.approvals.inbox() }) })
    expect(screen.queryByTestId(`approvals-row-${OLD.thread_id}`)).not.toBeInTheDocument()
    await waitForSelected(NEW.thread_id)
  })

  it('uses the pre-submit thread snapshot baseline when list and detail timestamps differ', async () => {
    const source = useInboxHandlers()
    const newerTimestamp = new Date(Date.parse(T_OLDEST) + 1000).toISOString()
    const preSubmitDetail = { ...DETAILS['run-old-1'], updated_at: newerTimestamp }
    server.use(http.get('*/v1/pipelines/run-old-1', () => HttpResponse.json(preSubmitDetail)))
    const user = userEvent.setup()
    const { queryClient } = renderInbox()
    await user.click(await screen.findByRole('button', { name: 'mock-revise' }))
    await user.click(await findRow(NEW.thread_id))
    // Even a newly fetched pre-submit detail must not mean the local revise finished.
    source.current = [NEW]
    await act(async () => { await queryClient.invalidateQueries() })
    expect(await findRow(OLD.thread_id)).toHaveTextContent('Agent working')
    // A stale list echo is also older than the captured thread baseline.
    source.current = [OLD, NEW]
    await act(async () => { await queryClient.invalidateQueries({ queryKey: queryKeys.approvals.inbox() }) })
    source.current = [NEW]
    await act(async () => { await queryClient.invalidateQueries({ queryKey: queryKeys.approvals.inbox() }) })
    expect(await findRow(OLD.thread_id)).toHaveTextContent('Agent working')
    server.use(http.get('*/v1/pipelines/run-old-1', () => HttpResponse.json({ ...preSubmitDetail, thread_status: 'error', pending_gate: null, interrupts: [] })))
    await act(async () => { await queryClient.invalidateQueries() })
    await waitFor(() => expect(screen.queryByText('Agent working — review will reopen')).not.toBeInTheDocument())
  })

  it('advances from a middle decision to its following gate, not the first gate', async () => {
    const rows = Array.from({ length: 4 }, (_, i) => gatedRun(`run-middle-${i}`, { kind: 'prompt_review', phase: 'test_planning', interruptId: `int-middle-${i}`, updatedAt: new Date(Date.now() - (4 - i) * 60_000).toISOString() }))
    const source = mutableListHandler(rows)
    server.use(source.handler, detailsHandler(Object.fromEntries(rows.map(row => [row.thread_id, gatedDetail(row)]))))
    const user = userEvent.setup()
    renderInbox('/runs/run-middle-1/reviews/int-middle-1?source=queue')
    await user.click(await screen.findByRole('button', { name: 'mock-approve' }))
    await waitForSelected('run-middle-2')
    expect(await findRow('run-middle-0')).toHaveAttribute('aria-selected', 'false')
  })

  it('keeps cached approval actions visible when a refresh fails', async () => {
    useInboxHandlers()
    const { queryClient } = renderInbox()
    await findRow('run-old-1')
    await screen.findByTestId('gate-module-mock')

    server.use(
      http.get('*/v1/pipelines', () =>
        HttpResponse.json({ detail: 'pipeline index temporarily unavailable' }, { status: 503 }),
      ),
    )
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.approvals.inbox() })
    })

    expect(await screen.findByText(/Showing cached data/)).toHaveTextContent(
      'pipeline index temporarily unavailable',
    )
    expect(await findRow('run-old-1')).toBeInTheDocument()
    expect(screen.getByTestId('gate-module-mock')).toBeInTheDocument()
  })

  it('? toggles the shortcuts overlay and Escape closes it', async () => {
    useInboxHandlers()
    const user = userEvent.setup()
    renderInbox()
    await findRow('run-old-1')

    await user.keyboard('?')
    const dialog = await screen.findByRole('dialog', { name: 'Keyboard shortcuts' })
    expect(within(dialog).getByText('Next gate')).toBeInTheDocument()
    expect(within(dialog).queryByText(/Approve/)).not.toBeInTheDocument()
    expect(within(dialog).queryByText(/Skip phase/)).not.toBeInTheDocument()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog', { name: 'Keyboard shortcuts' })).not.toBeInTheDocument()
  })

  it('shows the all-clear empty state with a ghost link to /runs', async () => {
    useInboxHandlers([RUN_BUSY]) // fleet alive, nothing gated
    renderInbox()

    const empty = await screen.findByTestId('approvals-empty')
    expect(empty).toHaveTextContent('All clear. No reviews pending.')
    expect(within(empty).getByRole('link', { name: 'Go to runs' })).toHaveAttribute(
      'href',
      '/runs',
    )
  })
})

describe('Sidebar review badge', () => {
  it('shows the pending-review count next to Tests and pulses when a gate is stale', async () => {
    useInboxHandlers()
    renderApp({ initialEntries: ['/'], authState: authenticatedState() })

    const badge = await screen.findByTestId('tests-review-nav-badge')
    expect(badge).toHaveTextContent('2')
    expect(badge).toHaveClass('dash-badge', 'pulse') // OLD waited > 15m
    expect(screen.getByRole('link', { name: /^Tests/ })).toContainElement(badge)
  })

  it('renders no badge when nothing is pending (and no pulse when gates are fresh)', async () => {
    // Default server handler: empty pipelines list.
    renderApp({ initialEntries: ['/'], authState: authenticatedState() })
    await screen.findByTestId('sidebar')
    await waitFor(() =>
      expect(screen.queryByTestId('tests-review-nav-badge')).not.toBeInTheDocument(),
    )
  })
})
