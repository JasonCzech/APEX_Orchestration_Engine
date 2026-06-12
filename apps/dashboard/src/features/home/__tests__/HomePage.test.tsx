/**
 * Home dashboard (plan UX 1.5): attention rail, active grid, recent table,
 * the sticky side panel (usage / drafts / health) and the first-launch hero.
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import type { PipelineDetail } from '@/api/hooks/useThreadState'
import { makeSummaries } from '@/features/runs/runsTestHandlers'
import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import {
  DRAFTS_FIXTURE,
  FLEET_FIXTURE,
  IDLE_RUN,
  USAGE_FIXTURE,
  draftsHandler,
  fleetHandler,
  usageFailsHandler,
  usageHandler,
} from './homeTestHandlers'

function useHomeHandlers({
  fleet = FLEET_FIXTURE,
  drafts = DRAFTS_FIXTURE,
  usageFails = false,
}: {
  fleet?: typeof FLEET_FIXTURE
  drafts?: typeof DRAFTS_FIXTURE
  usageFails?: boolean
} = {}) {
  server.use(
    fleetHandler(fleet),
    draftsHandler(drafts),
    usageFails ? usageFailsHandler() : usageHandler(),
  )
}

function renderHome() {
  return renderApp({ initialEntries: ['/'], authState: authenticatedState() })
}

describe('HomePage', () => {
  it('attention rail lists pending gates oldest-first with kind chips, ages and approvals deep links', async () => {
    useHomeHandlers()
    renderHome()

    const rail = await screen.findByTestId('home-attention')
    const oldGate = within(rail).getByTestId('home-gate-run-gated-old')
    expect(oldGate).toHaveAttribute('href', '/approvals/run-gated-old/int-old')
    expect(oldGate).toHaveClass('home-attention-row', 'warning')
    expect(within(oldGate).getByText('prompt_review')).toHaveClass('topbar-meta-chip', 'accent')
    expect(within(oldGate).getByText('test_planning')).toBeInTheDocument()
    expect(within(oldGate).getByText(/ago$/)).toBeInTheDocument()

    const newGate = within(rail).getByTestId('home-gate-run-gated-new')
    expect(newGate).toHaveAttribute('href', '/approvals/run-gated-new/int-new')
    expect(within(newGate).getByText('phase_review')).toHaveClass('topbar-meta-chip', 'info')

    // Oldest gate first (60m ago before 5m ago), failures after the gates.
    const rows = within(rail).getAllByRole('link')
    expect(rows.map((row) => row.getAttribute('data-testid'))).toEqual([
      'home-gate-run-gated-old',
      'home-gate-run-gated-new',
      'home-failure-run-failed',
    ])
  })

  it('attention rail lists failed runs with danger tone linking to the run', async () => {
    useHomeHandlers()
    renderHome()

    const failure = await screen.findByTestId('home-failure-run-failed')
    expect(failure).toHaveAttribute('href', '/runs/run-failed')
    expect(failure).toHaveClass('home-attention-row', 'danger')
    expect(within(failure).getByText('Broken nightly run')).toBeInTheDocument()
    expect(within(failure).getByText('failed')).toHaveClass('topbar-meta-chip', 'danger')
    expect(within(failure).getByText('10m ago')).toBeInTheDocument()
  })

  it('active runs grid renders cards with phase strips, status badges, phase captions and gate badges', async () => {
    useHomeHandlers()
    renderHome()

    const grid = await screen.findByTestId('home-active-grid')

    // busy + 2 interrupted, most recently updated first; error/idle excluded.
    const cards = within(grid).getAllByRole('link')
    expect(cards.map((card) => card.getAttribute('data-testid'))).toEqual([
      'home-active-run-busy',
      'home-active-run-gated-new',
      'home-active-run-gated-old',
    ])

    const busy = within(grid).getByTestId('home-active-run-busy')
    expect(busy).toHaveAttribute('href', '/runs/run-busy')
    expect(within(busy).getByText('Checkout latency soak')).toBeInTheDocument()
    expect(within(busy).getByText('busy')).toHaveClass('status-badge', 'accent')
    expect(within(busy).getByRole('group', { name: 'Phase progress' })).toBeInTheDocument()
    expect(within(busy).getByText('execution')).toHaveClass('home-active-phase', 'busy')
    expect(within(busy).queryByText(/^gate:/)).not.toBeInTheDocument()

    const gated = within(grid).getByTestId('home-active-run-gated-old')
    expect(within(gated).getByText('gate: prompt_review')).toHaveClass(
      'topbar-meta-chip',
      'warning',
    )
    expect(within(gated).getByText('test_planning')).toHaveClass('home-active-phase', 'gated')
  })

  it('recent runs table shows the last 8 by updated with a View all link to /runs', async () => {
    useHomeHandlers({ fleet: makeSummaries(12), drafts: [] })
    renderHome()

    const recent = await screen.findByTestId('home-recent')
    await waitFor(() =>
      expect(within(recent).getAllByTestId(/^home-recent-run-/)).toHaveLength(8),
    )
    expect(within(recent).getByRole('link', { name: 'View all' })).toHaveAttribute(
      'href',
      '/runs',
    )
    // Idle-only fleet: the attention rail and active grid collapse to nothing.
    expect(screen.queryByTestId('home-attention')).not.toBeInTheDocument()
    expect(screen.queryByTestId('home-active-grid')).not.toBeInTheDocument()
  })

  it('clicking a recent-run row navigates to the run detail', async () => {
    useHomeHandlers()
    const detail: PipelineDetail = {
      ...IDLE_RUN,
      values: {},
      interrupts: [],
    }
    server.use(
      http.get('*/v1/pipelines/:threadId', () => HttpResponse.json(detail)),
      http.get('*/threads/:threadId/runs', () => HttpResponse.json([])),
    )
    const user = userEvent.setup()
    const { router, queryClient } = renderHome()

    await user.click(await screen.findByTestId('home-recent-run-idle'))
    await waitFor(() =>
      expect(router.state.location.pathname).toBe('/runs/run-idle'),
    )
    // Let the run-detail page's queries settle inside THIS test's handlers
    // (otherwise the in-flight /threads/:id/runs fetch leaks past resetHandlers).
    await waitFor(() => expect(queryClient.isFetching()).toBe(0))
  })

  it('side panel shows the 7-day usage snapshot and the New Run CTA', async () => {
    useHomeHandlers()
    renderHome()

    const panel = await screen.findByTestId('home-panel')
    expect(within(panel).getByRole('link', { name: 'New Run' })).toHaveAttribute(
      'href',
      '/runs/new',
    )
    await waitFor(() =>
      expect(screen.getByTestId('home-usage-events')).toHaveTextContent('960'),
    )
    expect(screen.getByTestId('home-usage-succeeded')).toHaveTextContent('42')
    expect(screen.getByTestId('home-usage-failed')).toHaveTextContent('3')
    // 48 / 960 errors
    expect(screen.getByTestId('home-usage-error-rate')).toHaveTextContent('5.0%')
  })

  it('drafts panel lists resumable drafts (newest first) linking into the wizard', async () => {
    useHomeHandlers()
    renderHome()

    const drafts = await screen.findByTestId('home-drafts')
    const links = within(drafts).getAllByRole('link')
    expect(links.map((link) => link.getAttribute('href'))).toEqual([
      '/runs/new?draft=draft-1',
      '/runs/new?draft=draft-2',
    ])
    expect(within(drafts).getByText('Black Friday load test')).toBeInTheDocument()
  })

  it('hides empty panel sections and collapses the rail/grid on a quiet healthy fleet', async () => {
    useHomeHandlers({ fleet: [IDLE_RUN], drafts: [], usageFails: true })
    renderHome()

    // The recent table still renders the lone idle run...
    await screen.findByTestId('home-recent-run-idle')
    // ...but everything with nothing to say collapses to nothing.
    expect(screen.queryByTestId('home-attention')).not.toBeInTheDocument()
    expect(screen.queryByTestId('home-active-grid')).not.toBeInTheDocument()
    expect(screen.queryByTestId('home-drafts')).not.toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('home-usage')).not.toBeInTheDocument())
    // Health footer reflects the polled /v1/system/info (msw default: ok).
    await waitFor(() =>
      expect(screen.getByTestId('home-health')).toHaveTextContent('API: Connected'),
    )
  })

  it('renders the first-launch hero when there are no runs and no drafts', async () => {
    // Default server handler already returns an empty pipelines list.
    server.use(draftsHandler([]), usageHandler(USAGE_FIXTURE))
    renderHome()

    const hero = await screen.findByTestId('home-hero')
    expect(within(hero).getByRole('heading', { name: 'Start your first pipeline' })).toBeVisible()
    expect(within(hero).getByRole('link', { name: 'New Run' })).toHaveAttribute(
      'href',
      '/runs/new',
    )
    expect(screen.queryByTestId('home-panel')).not.toBeInTheDocument()
  })

  it('keeps the normal layout when runs are gone but a resumable draft exists', async () => {
    useHomeHandlers({ fleet: [], drafts: [DRAFTS_FIXTURE[0]!] })
    renderHome()

    const drafts = await screen.findByTestId('home-drafts')
    expect(within(drafts).getByTestId('home-draft-draft-1')).toHaveAttribute(
      'href',
      '/runs/new?draft=draft-1',
    )
    expect(screen.queryByTestId('home-hero')).not.toBeInTheDocument()
    expect(screen.getByText(/No runs yet/)).toBeInTheDocument()
  })
})
