import { cloneElement, isValidElement, type ReactElement, type ReactNode } from 'react'

import { cleanup, screen } from '@testing-library/react'
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

  it.each([
    ['/', 'ACC add-a-line active ramp'],
    ['/runs', 'ACC add-a-line active ramp'],
    ['/runs/run-active', 'ACC add-a-line active ramp'],
    ['/runs/run-active/artifacts/exec-report', /"tps_avg": 84\.7/],
    ['/approvals', 'PI 26.3 workload prompt review'],
    ['/environments', 'PERFHYPER2-DR'],
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
    renderApp({
      initialEntries: [path],
      authState: authenticatedState(),
      // This route specifically verifies the dev-data system response. Other
      // smoke routes retain the static session seed and avoid unrelated IO.
      seedSystemInfo: path !== '/admin/system',
    })

    expect(await screen.findByText(expectedText, {}, { timeout: 4_000 })).toBeInTheDocument()
  })

  it.each([
    ['/approvals', 'Approvals'],
    ['/approvals/run-prompt-review/int-prompt', 'Approval'],
    ['/runs', 'Test History'],
    ['/runs/new?draft=draft-esim', 'New Test'],
    ['/runs/compare?ids=run-baseline-a,run-baseline-b', 'Compare Runs'],
    ['/runs/run-active', 'Run Detail'],
    ['/runs/run-active/phases/execution', 'Phase Detail'],
    ['/runs/run-active/timeline', 'Run Timeline'],
    ['/runs/run-active/artifacts/exec-report', 'Artifact Viewer'],
    ['/prompts', 'Prompts'],
    ['/prompts/phase/story_analysis%2Fsystem', 'Prompt'],
    ['/prompts/phase/story_analysis%2Fsystem/versions/2', 'Prompt Version'],
    ['/prompts/phase/story_analysis%2Fsystem/playground', 'Prompt Playground'],
    ['/golden-configs', 'Golden Configs'],
    ['/golden-configs/asst-release', 'Golden Config'],
    ['/work-items', 'Tickets/Defects'],
    ['/work-items/saved', 'Saved Queries'],
    ['/work-items/jira_align/feature%3A583?connection_id=conn-align', 'Work Item'],
    ['/environments', 'Environment Configurations'],
    ['/environments/env-perf', 'Environment'],
    ['/context', 'Context'],
    ['/analytics', 'Analytics'],
    ['/logs', 'Logs'],
    ['/apex-load', 'APEX Load'],
    ['/admin/connections', 'Connections'],
    ['/admin/connections/conn-align', 'Connection'],
    ['/admin/consumers', 'Consumers'],
    ['/admin/consumers/cons-ci', 'Consumer'],
    ['/admin/system', 'System'],
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
