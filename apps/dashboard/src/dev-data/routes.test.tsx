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
    ['/', 'Checkout latency regression'],
    ['/runs', 'Checkout latency regression'],
    ['/runs/run-busy', 'Checkout latency regression'],
    ['/runs/run-busy/artifacts/exec-report', /"tps_avg": 148\.2/],
    ['/approvals', 'Nightly soak prompt review'],
    ['/environments', 'staging'],
    ['/work-items/saved', 'Open payment stories'],
    ['/context?tab=documents', 'checkout-spec.pdf'],
    ['/prompts', 'story_analysis/system'],
    ['/golden-configs', 'Release Gate Soak'],
    ['/analytics', 'Agents / runs'],
    ['/logs?q=gateway', 'gateway 502 spike during checkout ramp'],
    ['/admin/connections', 'jira-prod'],
    ['/admin/consumers', 'Read Only Reviewer'],
    ['/admin/system', 'dev-dummy'],
  ])('renders %s from dummy data', async (path, expectedText) => {
    renderApp({ initialEntries: [path], authState: authenticatedState() })

    expect(await screen.findByText(expectedText, {}, { timeout: 4_000 })).toBeInTheDocument()
  })
})
