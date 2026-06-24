/**
 * Home dashboard: metric cards, approval queue, recent runs, draft resume,
 * and the first-launch hero.
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
  BUSY_RUN,
  DRAFTS_FIXTURE,
  FAILED_RUN,
  FLEET_FIXTURE,
  IDLE_RUN,
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
    http.get('*/threads/:threadId/runs', () => HttpResponse.json([])),
  )
}

function renderHome() {
  return renderApp({ initialEntries: ['/'], authState: authenticatedState() })
}

describe('HomePage', () => {
  it('renders the metric cards and release signal from the available fleet proxy data', async () => {
    useHomeHandlers()
    renderHome()

    expect(
      await screen.findByRole('heading', { level: 1, name: 'Pipeline Operation Dashboard' }),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('heading', { level: 2, name: /Pipeline Operations? Dashboard/ }),
    ).not.toBeInTheDocument()
    const totalCard = (await screen.findByText('Total Runs')).closest('article')
    expect(totalCard).not.toBeNull()
    expect(within(totalCard as HTMLElement).getByText('5')).toBeInTheDocument()
    const activeCard = screen.getByText('Active').closest('article')
    expect(activeCard).not.toBeNull()
    expect(within(activeCard as HTMLElement).getByText('3')).toBeInTheDocument()
    const failureCard = screen.getByText('Failures').closest('article')
    expect(failureCard).not.toBeNull()
    expect(within(failureCard as HTMLElement).getByText('1')).toBeInTheDocument()
    const goCard = screen.getByText('GO Verdicts').closest('article')
    expect(goCard).not.toBeNull()
    expect(within(goCard as HTMLElement).getByText('1')).toBeInTheDocument()
    expect(within(goCard as HTMLElement).getByText('idle run proxy')).toBeInTheDocument()

    const release = screen.getByRole('heading', { name: 'Release Signal' }).closest('article')
    expect(release).not.toBeNull()
    expect(within(release as HTMLElement).getByText('GO · 1')).toBeInTheDocument()
    expect(within(release as HTMLElement).getByText('Conditional · 2')).toBeInTheDocument()
    expect(within(release as HTMLElement).getByText('NO-GO · 1')).toBeInTheDocument()
  })

  it('lists pending approvals oldest-first with deep links into the queue', async () => {
    useHomeHandlers()
    renderHome()

    const approvals = await screen.findByTestId('home-approvals-list')
    const links = within(approvals).getAllByRole('link')
    expect(links.map((link) => link.getAttribute('href'))).toEqual([
      '/approvals/run-gated-old/int-old',
      '/approvals/run-gated-new/int-new',
    ])

    const oldGate = links[0] as HTMLElement
    expect(within(oldGate).getByText('prompt_review')).toHaveClass('topbar-meta-chip', 'warning')
    expect(within(oldGate).getByText('Oldest gated run')).toBeInTheDocument()
    expect(within(oldGate).getByText('test_planning')).toBeInTheDocument()

    const newGate = links[1] as HTMLElement
    expect(within(newGate).getByText('phase_review')).toHaveClass('topbar-meta-chip', 'warning')
    expect(within(newGate).getByText('Newest gated run')).toBeInTheDocument()
  })

  it('renders recent runs and navigates to the run detail from the run link', async () => {
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

    const row = await screen.findByTestId('home-recent-run-idle')
    expect(within(row).getByText('Completed smoke run')).toBeInTheDocument()
    expect(within(row).getByText('idle')).toHaveClass('status-badge', 'success')

    await user.click(within(row).getByRole('link', { name: 'Completed smoke run' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/runs/run-idle'))
    await waitFor(() => expect(queryClient.isFetching()).toBe(0))
  })

  it('caps recent runs at eight entries and keeps the View all link wired to /runs', async () => {
    useHomeHandlers({ fleet: makeSummaries(12), drafts: [] })
    renderHome()

    const recent = await screen.findByTestId('home-recent')
    await waitFor(() =>
      expect(within(recent).getAllByTestId(/^home-recent-run-/)).toHaveLength(8),
    )
    expect(within(recent).getByRole('link', { name: 'View all' })).toHaveAttribute('href', '/runs')
  })

  it('lists resumable drafts newest-first and hides the hero while drafts exist', async () => {
    useHomeHandlers({ fleet: [], drafts: [DRAFTS_FIXTURE[0]!] })
    renderHome()

    const drafts = await screen.findByTestId('home-drafts')
    const links = within(drafts).getAllByRole('link')
    expect(links.map((link) => link.getAttribute('href'))).toEqual(['/runs/new?draft=draft-1'])
    expect(within(drafts).getByText('Black Friday load test')).toBeInTheDocument()
    expect(screen.queryByTestId('home-hero')).not.toBeInTheDocument()
    expect(screen.getByText('No runs yet.')).toBeInTheDocument()
  })

  it('shows empty states for approvals and recent runs on a quiet healthy fleet', async () => {
    useHomeHandlers({ fleet: [IDLE_RUN], drafts: [], usageFails: true })
    renderHome()

    expect(await screen.findByTestId('home-recent-run-idle')).toBeInTheDocument()
    expect(screen.getByText('No pending approvals right now.')).toBeInTheDocument()
    expect(screen.queryByTestId('home-drafts')).not.toBeInTheDocument()
    expect(
      screen.getAllByRole('link', { name: 'New Test' }).every((link) => link.getAttribute('href') === '/runs/new'),
    ).toBe(true)
  })

  it('renders the first-launch hero when there are no runs and no drafts', async () => {
    server.use(fleetHandler([]), draftsHandler([]), usageHandler())
    renderHome()

    const hero = await screen.findByTestId('home-hero')
    expect(within(hero).getByRole('heading', { name: 'Start your first pipeline' })).toBeVisible()
    expect(within(hero).getByRole('link', { name: 'New Test' })).toHaveAttribute(
      'href',
      '/runs/new',
    )
  })

  it('renders the execution health panel with fleet and phase totals', async () => {
    useHomeHandlers()
    renderHome()

    const panel = await screen.findByRole('heading', { name: 'Execution Health' })
    const card = panel.closest('article')
    expect(card).not.toBeNull()
    const activeBlock = within(card as HTMLElement).getByText('Active pipelines').closest('div')
    expect(activeBlock).not.toBeNull()
    expect(within(activeBlock as HTMLElement).getByText('3')).toBeInTheDocument()
    const approvalsBlock = within(card as HTMLElement).getByText('Pending approvals').closest('div')
    expect(approvalsBlock).not.toBeNull()
    expect(within(approvalsBlock as HTMLElement).getByText('2')).toBeInTheDocument()
    const successBlock = within(card as HTMLElement).getByText('Phases succeeded').closest('div')
    expect(successBlock).not.toBeNull()
    expect(within(successBlock as HTMLElement).getByText('42')).toBeInTheDocument()
    const failedBlock = within(card as HTMLElement).getByText('Phases failed').closest('div')
    expect(failedBlock).not.toBeNull()
    expect(within(failedBlock as HTMLElement).getByText('3')).toBeInTheDocument()
  })

  it('keeps failed and busy runs represented in the recent table', async () => {
    useHomeHandlers({ fleet: [FAILED_RUN, BUSY_RUN, IDLE_RUN], drafts: [] })
    renderHome()

    const recent = await screen.findByTestId('home-recent')
    expect(within(recent).getByText('Broken nightly run')).toBeInTheDocument()
    expect(within(recent).getByText('Checkout latency soak')).toBeInTheDocument()
    expect(within(recent).getByText('error')).toHaveClass('status-badge', 'danger')
    expect(within(recent).getByText('busy')).toHaveClass('status-badge', 'accent')
  })
})
