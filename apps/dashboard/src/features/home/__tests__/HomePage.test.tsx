/**
 * Home dashboard: metric cards, approval queue, recent runs, draft resume,
 * and the first-launch hero.
 */
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import type { PipelineDetail } from '@/api/hooks/useThreadState'
import { queryKeys } from '@/api/queryKeys'
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

function pipelinesWithControlledApprovals(
  items: typeof FLEET_FIXTURE,
  approvalsShouldFail: () => boolean,
) {
  return http.get('*/v1/pipelines', ({ request }) => {
    const url = new URL(request.url)
    const status = url.searchParams.get('status')
    if (status === 'interrupted' && approvalsShouldFail()) {
      return HttpResponse.json({ detail: 'approvals offline' }, { status: 500 })
    }
    const filtered = status ? items.filter((run) => run.thread_status === status) : items
    return HttpResponse.json({
      items: filtered,
      limit: Number(url.searchParams.get('limit') ?? '20'),
      offset: Number(url.searchParams.get('offset') ?? '0'),
    })
  })
}

describe('HomePage', () => {
  it('resolves application names in Recent runs using the scoped catalog', async () => {
    useHomeHandlers()
    server.use(http.get('*/v1/catalog/applications', () => HttpResponse.json([{ id: 'app-storefront', name: 'Storefront' }])))
    renderHome()
    expect(await screen.findByText('Storefront', { selector: '.home-recent-row td span' })).toHaveAttribute('title', 'app-storefront')
  })

  it('renders the metric cards and release signal from the available fleet proxy data', async () => {
    useHomeHandlers()
    renderHome()

    expect(
      await screen.findByRole('heading', { level: 1, name: 'Test operations dashboard' }),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('heading', { level: 2, name: /Pipeline Operations? Dashboard/ }),
    ).not.toBeInTheDocument()
    expect(await screen.findByRole('heading', { level: 2, name: 'Needs your attention' })).toBeInTheDocument()
    const totalCard = (await screen.findByText('Recent runs', { selector: '.home-metric-label' })).closest('article')
    expect(totalCard).not.toBeNull()
    expect(within(totalCard as HTMLElement).getByText('5')).toBeInTheDocument()
    expect(within(totalCard as HTMLElement).getByText('recent accessible runs')).toBeInTheDocument()
    const activeCard = screen.getByText('Active').closest('article')
    expect(activeCard).not.toBeNull()
    expect(within(activeCard as HTMLElement).getByText('3')).toBeInTheDocument()
    const failureCard = screen.getByText('Runs needing investigation').closest('article')
    expect(failureCard).not.toBeNull()
    expect(within(failureCard as HTMLElement).getByText('1')).toBeInTheDocument()
    expect(screen.queryByText('Tests Passed')).not.toBeInTheDocument()

    const release = screen.getByRole('heading', { level: 2, name: 'Test results' }).closest('article')
    expect(release).not.toBeNull()
    expect(within(release as HTMLElement).getByText('Passed · 0')).toBeInTheDocument()
    expect(within(release as HTMLElement).getByText('Not available · 5')).toBeInTheDocument()
    expect(within(release as HTMLElement).getByText('Failed · 0')).toBeInTheDocument()
  })

  it('lists pending approvals oldest-first with deep links into the queue', async () => {
    useHomeHandlers()
    renderHome()

    const approvals = await screen.findByTestId('home-approvals-list')
    const links = within(approvals).getAllByRole('link')
    expect(links.map((link) => link.getAttribute('href'))).toEqual([
      '/runs/run-gated-old/reviews/int-old?source=home',
      '/runs/run-gated-new/reviews/int-new?source=home',
    ])

    const oldGate = links[0] as HTMLElement
    expect(within(oldGate).getByText('Review prompts')).toHaveClass('topbar-meta-chip', 'warning')
    expect(within(oldGate).getByText('Oldest gated run')).toBeInTheDocument()
    expect(within(oldGate).getByText('Test Planning')).toBeInTheDocument()

    const newGate = links[1] as HTMLElement
    expect(within(newGate).getByText('Review phase results')).toHaveClass('topbar-meta-chip', 'warning')
    expect(within(newGate).getByText('Newest gated run')).toBeInTheDocument()
  })

  it('does not report a clear approvals queue when its initial request fails, and retries', async () => {
    let approvalsFail = true
    server.use(
      pipelinesWithControlledApprovals(FLEET_FIXTURE, () => approvalsFail),
      draftsHandler([]),
      usageHandler(),
    )
    const user = userEvent.setup()
    renderHome()

    const panel = await screen.findByRole('region', { name: 'Needs your attention' })
    const alert = within(panel).getByRole('alert')
    expect(alert).toHaveTextContent('Reviews unavailable')
    expect(within(panel).queryByText('No pending reviews right now.')).not.toBeInTheDocument()

    approvalsFail = false
    await user.click(within(alert).getByRole('button', { name: 'Retry' }))

    expect(await within(panel).findByTestId('home-approvals-list')).toBeInTheDocument()
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
    expect(within(row).getByText('Orchestration stopped')).toHaveClass('status-badge', 'neutral')

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

  it('does not show the first-launch hero when draft lookup fails, and restores drafts on retry', async () => {
    let draftsFail = true
    server.use(
      fleetHandler([]),
      usageHandler(),
      http.get('*/v1/drafts', () =>
        draftsFail
          ? HttpResponse.json({ detail: 'drafts offline' }, { status: 500 })
          : HttpResponse.json([DRAFTS_FIXTURE[0]!]),
      ),
    )
    const user = userEvent.setup()
    renderHome()

    const drafts = await screen.findByTestId('home-drafts')
    const alert = within(drafts).getByRole('alert')
    expect(alert).toHaveTextContent('Saved drafts unavailable')
    expect(screen.queryByTestId('home-hero')).not.toBeInTheDocument()

    draftsFail = false
    await user.click(within(alert).getByRole('button', { name: 'Retry' }))

    expect(await within(drafts).findByText('Black Friday load test')).toBeInTheDocument()
    expect(within(drafts).getByRole('link')).toHaveAttribute('href', '/runs/new?draft=draft-1')
  })

  it('keeps cached approvals and drafts visible when their refreshes fail', async () => {
    useHomeHandlers()
    const { queryClient } = renderHome()

    const approvalPanel = await screen.findByRole('region', { name: 'Needs your attention' })
    const drafts = await screen.findByTestId('home-drafts')
    expect(within(approvalPanel).getByText('Oldest gated run')).toBeInTheDocument()
    expect(within(drafts).getByText('Black Friday load test')).toBeInTheDocument()

    server.use(
      pipelinesWithControlledApprovals(FLEET_FIXTURE, () => true),
      http.get('*/v1/drafts', () =>
        HttpResponse.json({ detail: 'draft refresh failed' }, { status: 500 }),
      ),
    )
    await act(async () => {
      await queryClient.refetchQueries({
        queryKey: queryKeys.approvals.inbox(),
        exact: true,
      })
    })

    await waitFor(() =>
      expect(within(approvalPanel).getByText(/Showing cached data/)).toBeInTheDocument(),
    )
    expect(within(approvalPanel).getByText('Oldest gated run')).toBeInTheDocument()
    expect(within(approvalPanel).getByRole('button', { name: 'Retry' })).toBeInTheDocument()

    await act(async () => {
      await queryClient.refetchQueries({
        queryKey: queryKeys.drafts.list(),
        exact: true,
      })
    })

    await waitFor(() =>
      expect(within(drafts).getByText(/Showing cached data/)).toBeInTheDocument(),
    )
    expect(within(drafts).getByText('Black Friday load test')).toBeInTheDocument()
    expect(within(drafts).getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('shows empty states for approvals and recent runs on a quiet healthy fleet', async () => {
    useHomeHandlers({ fleet: [IDLE_RUN], drafts: [], usageFails: true })
    renderHome()

    expect(await screen.findByTestId('home-recent-run-idle')).toBeInTheDocument()
    expect(screen.getByText('No pending reviews right now.')).toBeInTheDocument()
    expect(screen.queryByTestId('home-drafts')).not.toBeInTheDocument()
    expect(
      screen.getAllByRole('link', { name: 'New test' }).every((link) => link.getAttribute('href') === '/runs/new'),
    ).toBe(true)
  })

  it('renders the first-launch hero when there are no runs and no drafts', async () => {
    server.use(fleetHandler([]), draftsHandler([]), usageHandler())
    renderHome()

    const hero = await screen.findByTestId('home-hero')
    expect(within(hero).getByRole('heading', { name: 'Start your first test' })).toBeVisible()
    expect(within(hero).getByRole('link', { name: 'New test' })).toHaveAttribute(
      'href',
      '/runs/new',
    )
  })

  it('renders the execution health panel with fleet and phase totals', async () => {
    useHomeHandlers()
    renderHome()

    const panel = await screen.findByRole('heading', { level: 2, name: 'Execution health' })
    const card = panel.closest('article')
    expect(card).not.toBeNull()
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
    expect(within(recent).getByText('Orchestration failed')).toHaveClass('status-badge', 'danger')
    expect(within(recent).getByText('Running')).toHaveClass('status-badge', 'accent')
  })
})
