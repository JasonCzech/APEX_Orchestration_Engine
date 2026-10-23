import { cloneElement, isValidElement, type ReactElement, type ReactNode } from 'react'

import { cleanup, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DEV_DATA_STORAGE_KEY, setDevDataEnabled } from '@/dev-data'
import { authenticatedState, renderApp } from '@/test/render'

vi.mock('@uiw/react-codemirror', async () => {
  const { createElement } = await import('react')
  return {
    default: ({ value }: { value: string }) =>
      createElement('pre', { 'data-testid': 'codemirror' }, value),
  }
})

vi.mock('recharts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('recharts')>()
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactNode }) =>
      isValidElement(children)
        ? cloneElement(children as ReactElement<{ width?: number; height?: number }>, {
            width: 600,
            height: 180,
          })
        : children,
  }
})

describe('dev-data route smoke', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_APEX_DEV_AUTH', 'true')
    vi.stubEnv('VITE_APEX_DEV_API_KEY', 'dev-key-local')
    setDevDataEnabled(true)
  })

  afterEach(() => {
    cleanup()
    setDevDataEnabled(false)
    window.localStorage.removeItem(DEV_DATA_STORAGE_KEY)
    vi.unstubAllEnvs()
  })

  it('renders the mirrored Load ledger with execution identities and analysis links', async () => {
    renderApp({ initialEntries: ['/apex-load'], authState: authenticatedState() })
    const executions = await screen.findByRole('region', { name: 'Load executions' })
    expect(await within(executions).findByText('ACC add-a-line certification 26.3')).toBeInTheDocument()
    expect(within(executions).getAllByRole('link', { name: 'Open in APEX Load' })[0]).toHaveAttribute(
      'href',
      expect.stringContaining('native-run-2603'),
    )
    expect(within(executions).getAllByRole('link', { name: 'Open analysis' })).toHaveLength(4)
    expect(within(executions).getAllByRole('link', { name: 'Open analysis' })[0]).toHaveAttribute('href', '/runs/run-baseline-a')
  })

  it.each([
    ['/', 'ACC add-a-line active ramp — PERFHYPER2'],
    ['/runs', 'ACC add-a-line active ramp — PERFHYPER2'],
    ['/runs/run-active', 'ACC add-a-line active ramp — PERFHYPER2'],
    ['/runs/run-active/artifacts/exec-report', /"tps_avg": 84\.7/],
    ['/approvals', 'OPUS upgrade orders PI 26.4 — T4 workload review'],
    ['/environments', 'PERF TLG'],
    ['/work-items/saved', 'ACC add-a-line release scope'],
    ['/context?tab=documents', 'acc-add-line-architecture.pdf'],
    ['/prompts', 'story_analysis/system'],
    ['/golden-configs', 'ACC add-a-line Release Certification'],
    ['/analytics', 'Agents / runs'],
    ['/apex-load', 'IBM MQ backlog recovery'],
    ['/admin/connections', 'Jira Align Portfolio'],
    ['/admin/consumers', 'Nightly Certification'],
    ['/admin/system', 'offline-demo'],
  ])('renders %s from dummy data', async (path, expectedText) => {
    const user = userEvent.setup()
    renderApp({
      initialEntries: [path],
      authState: authenticatedState(),
      // This route specifically verifies the dev-data system response. Other
      // smoke routes retain the static session seed and avoid unrelated IO.
      seedSystemInfo: path !== '/admin/system',
    })

    if (path === '/apex-load') {
      await user.click(await screen.findByRole('tab', { name: 'Scenarios & schedules' }))
    }

    expect((await screen.findAllByText(expectedText, {}, { timeout: 4_000 }))[0]).toBeInTheDocument()
  })

  it.each([
    ['/approvals', 'Tests'],
    ['/approvals/run-prompt-review/int-prompt', 'Review'],
    ['/runs', 'Tests'],
    ['/runs/new?draft=draft-esim', 'New test'],
    ['/runs/compare?ids=run-baseline-a,run-baseline-b', 'Compare runs'],
    ['/runs/run-active', 'Run detail'],
    ['/runs/run-active/phases/execution', 'Run detail'],
    ['/runs/run-active/timeline', 'Run detail'],
    ['/runs/run-active/artifacts/exec-report', 'Artifact viewer'],
    ['/prompts', 'Prompts'],
    ['/prompts/phase/story_analysis%2Fsystem', 'Prompt'],
    ['/prompts/phase/story_analysis%2Fsystem/versions/2', 'Prompt version'],
    ['/prompts/phase/story_analysis%2Fsystem/playground', 'Template preview'],
    ['/golden-configs', 'Test presets'],
    ['/golden-configs/asst-release', 'Test preset'],
    ['/work-items', 'Tickets'],
    ['/work-items/saved', 'Saved queries'],
    ['/work-items/jira_align/feature%3A583?connection_id=conn-align', 'Ticket'],
    ['/environments', 'Applications & environments'],
    ['/environments/env-perf', 'Environment'],
    ['/context', 'Knowledge & documents'],
    ['/evidence', 'Evidence'],
    ['/analytics', 'Agent analytics'],
    ['/logs', 'Logs'],
    ['/apex-load', 'APEX Load'],
    ['/admin/connections', 'Connections'],
    ['/admin/connections/conn-align', 'Connection'],
    ['/admin/consumers', 'API consumers'],
    ['/admin/consumers/cons-ci', 'Consumer'],
    ['/admin/system', 'System status'],
    ['/settings', 'Settings'],
  ])('mounts every production dashboard route %s', async (path, heading) => {
    renderApp({
      initialEntries: [path],
      authState: authenticatedState(),
      seedSystemInfo: path !== '/admin/system',
    })

    expect(await screen.findByRole('heading', { name: heading, level: 1 }, { timeout: 4_000 })).toBeInTheDocument()
  })
})
