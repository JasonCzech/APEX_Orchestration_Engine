import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import type { AifxGatePreview, PhaseName } from '@apex/pipeline-events'

import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { AifxGateCard } from '../AifxGateCard'

const SHA = '0123456789abcdef0123456789abcdef01234567'
const GATE: AifxGatePreview = {
  status: 'pinned',
  snapshot_sha256: 'a'.repeat(64),
  sources: [
    { role: 'global-rules', repo: 'aifx/global-rules', ref: 'main', commit_sha: SHA, web_url: `https://ghe.example.test/aifx/global-rules/tree/${SHA}`, application_name: null },
    { role: 'application-context', repo: 'aifx/apm1-acb', ref: 'main', commit_sha: SHA, web_url: null, application_name: 'Storefront' },
  ],
  entries: [{ id: 'evidence-first', type: 'rule', title: 'Evidence first', repo: 'aifx/global-rules', path: 'rules/e.md' }],
  issues: [],
}

function renderCard(aifx: AifxGatePreview = GATE, threadId?: string, phase?: PhaseName) {
  return render(
    <QueryClientProvider client={createTestQueryClient()}>
      <AifxGateCard aifx={aifx} threadId={threadId} phase={phase} />
    </QueryClientProvider>,
  )
}

function snapshotEntry(id: string, title: string, phases: 'all' | PhaseName[], content: string) {
  return {
    id,
    role: 'global-rules',
    application_id: null,
    repo: 'aifx/global-rules',
    type: 'rule',
    phases,
    title,
    description: null,
    path: `rules/${id}.md`,
    content_sha256: 'b'.repeat(64),
    content,
  }
}

const SNAPSHOT_ENTRIES = [
  snapshotEntry('evidence-first', 'Evidence first', 'all', 'Cite evidence for every target.'),
  snapshotEntry('plan-scope', 'Plan scope', ['test_planning'], 'Scope the plan to the story.'),
  snapshotEntry('report-only', 'Report only', ['reporting'], 'Not for this phase.'),
]

function serveSnapshot() {
  server.use(
    http.get('*/v1/pipelines/:id/aifx', () =>
      HttpResponse.json({
        status: 'pinned',
        snapshot: { schema_version: 1, status: 'pinned', snapshot_sha256: 'a'.repeat(64), issues: [], sources: [], entries: SNAPSHOT_ENTRIES },
      }),
    ),
  )
}

describe('AifxGateCard', () => {
  it('lists pinned sources at their commits and this phase’s entries', () => {
    renderCard()
    const card = screen.getByTestId('gate-aifx')
    expect(within(card).getByText('aifx/global-rules')).toBeInTheDocument()
    expect(within(card).getByText('Storefront · aifx/apm1-acb')).toBeInTheDocument()
    expect(within(card).getByRole('link', { name: '0123456' })).toHaveAttribute('href', GATE.sources[0]!.web_url!)
    expect(within(card).getByText(/This phase follows 1 entry: Evidence first/)).toBeInTheDocument()
    expect(within(card).queryByText('Show pinned content (read-only)')).not.toBeInTheDocument()
  })

  it('loads the pinned content read-only on request', async () => {
    let reads = 0
    server.use(
      http.get('*/v1/pipelines/:id/aifx', () => {
        reads += 1
        return HttpResponse.json({
          status: 'pinned',
          snapshot: {
            schema_version: 1,
            status: 'pinned',
            snapshot_sha256: 'a'.repeat(64),
            issues: [],
            sources: [],
            entries: [
              { id: 'evidence-first', role: 'global-rules', application_id: null, repo: 'aifx/global-rules', type: 'rule', phases: 'all', title: 'Evidence first', description: null, path: 'rules/e.md', content_sha256: 'b'.repeat(64), content: 'Cite evidence for every target.' },
              { id: 'other', role: 'global-rules', application_id: null, repo: 'aifx/global-rules', type: 'rule', phases: ['reporting'], title: 'Other phase', description: null, path: 'rules/o.md', content_sha256: 'c'.repeat(64), content: 'Not for this phase.' },
            ],
          },
        })
      }),
    )
    const user = userEvent.setup()
    renderCard(GATE, 'thread-1')
    expect(reads).toBe(0)
    await user.click(screen.getByText('Show pinned content (read-only)'))
    expect(await screen.findByText('Cite evidence for every target.')).toBeInTheDocument()
    expect(screen.queryByText('Not for this phase.')).not.toBeInTheDocument()
    expect(screen.getAllByTestId('gate-aifx-entry')).toHaveLength(1)
    expect(reads).toBe(1)
  })

  it('flags a degraded launch', () => {
    renderCard({ ...GATE, status: 'degraded', issues: ['aifx/apm1-acb — no response within 30s'] })
    expect(screen.getByRole('status')).toHaveTextContent('Launched without some sources (1 problem)')
  })

  it('lists every degraded issue and how many more were left out', () => {
    renderCard({ ...GATE, status: 'degraded', issues: ['aifx/apm1-acb — no response within 30s', 'aifx/global-rules — manifest invalid'], issue_count: 5 })
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('Launched without some sources (5 problems)')
    expect(within(status).getByText('aifx/apm1-acb — no response within 30s')).toBeInTheDocument()
    expect(within(status).getByText('aifx/global-rules — manifest invalid')).toBeInTheDocument()
    expect(within(status).getAllByRole('listitem')).toHaveLength(2)
    expect(within(status).getByText('…and 3 more.')).toBeInTheDocument()
  })

  it('omits the “more” line when every degraded issue is shown', () => {
    renderCard({ ...GATE, status: 'degraded', issues: ['aifx/apm1-acb — no response within 30s'], issue_count: 1 })
    expect(screen.queryByText(/…and \d+ more\./)).not.toBeInTheDocument()
  })

  it('reports the full entry count when the listed entries are truncated', () => {
    renderCard({ ...GATE, entry_count: 70 })
    expect(
      screen.getByText((_, element) => element?.tagName === 'P' && element.textContent === 'This phase follows 70 entries (first 1 shown): Evidence first, …'),
    ).toBeInTheDocument()
  })

  it('falls back to the listed entries when entry_count is absent', () => {
    const entries: AifxGatePreview['entries'] = [...GATE.entries, { id: 'plan-scope', type: 'rule', title: 'Plan scope', repo: 'aifx/global-rules', path: 'rules/plan-scope.md' }]
    renderCard({ ...GATE, entries })
    expect(
      screen.getByText((_, element) => element?.tagName === 'P' && element.textContent === 'This phase follows 2 entries: Evidence first, Plan scope.'),
    ).toBeInTheDocument()
  })

  it('shows every snapshot entry for the phase when the gate list is truncated', async () => {
    serveSnapshot()
    const user = userEvent.setup()
    renderCard({ ...GATE, entry_count: 2 }, 'thread-1', 'test_planning')
    await user.click(screen.getByText('Show pinned content (read-only)'))
    expect(await screen.findByText('Scope the plan to the story.')).toBeInTheDocument()
    expect(screen.getByText('Cite evidence for every target.')).toBeInTheDocument()
    expect(screen.queryByText('Not for this phase.')).not.toBeInTheDocument()
    expect(screen.getAllByTestId('gate-aifx-entry')).toHaveLength(2)
  })

  it('shows only the listed entries when the gate list is complete, even with a phase', async () => {
    serveSnapshot()
    const user = userEvent.setup()
    renderCard({ ...GATE, entry_count: 1 }, 'thread-1', 'test_planning')
    await user.click(screen.getByText('Show pinned content (read-only)'))
    expect(await screen.findByText('Cite evidence for every target.')).toBeInTheDocument()
    expect(screen.queryByText('Scope the plan to the story.')).not.toBeInTheDocument()
    expect(screen.getAllByTestId('gate-aifx-entry')).toHaveLength(1)
  })
})
