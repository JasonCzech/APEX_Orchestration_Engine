import { cloneElement, isValidElement, type ReactNode } from 'react'

import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { EngineStrip } from '../EngineStrip'

import { engineSamples } from './liveFixtures'

// jsdom has no layout, so ResponsiveContainer would render nothing at 0x0.
// Replace it with a pass-through that pins the chart size (recharts' own
// test convention); everything else stays the real library.
vi.mock('recharts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('recharts')>()
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactNode }) =>
      isValidElement(children)
        ? cloneElement(children as React.ReactElement<{ width?: number; height?: number }>, {
            width: 600,
            height: 180,
          })
        : children,
  }
})

describe('EngineStrip', () => {
  it('renders the four command-metric pills from the latest sample plus the area chart', () => {
    const samples = engineSamples(20)
    render(<EngineStrip samples={samples} latest={samples[samples.length - 1]} />)

    const strip = screen.getByTestId('engine-strip')
    expect(within(strip).getByTestId('engine-pill-vusers')).toHaveTextContent('50')
    expect(within(strip).getByTestId('engine-pill-tps')).toHaveTextContent('39.5') // 30 + 19*0.5
    expect(within(strip).getByTestId('engine-pill-error_rate')).toHaveTextContent('0.42%')
    expect(within(strip).getByTestId('engine-pill-p95_ms')).toHaveTextContent('229 ms')
    expect(within(strip).getByTestId('engine-status')).toHaveTextContent('running · 19%')

    // Chart actually mounts an SVG surface from the sample series.
    expect(strip.querySelector('.recharts-surface')).toBeInTheDocument()
  })

  it('degrades per-metric with an em dash when live_stats is null', () => {
    render(
      <EngineStrip
        samples={[{ status: 'provisioning', progress_pct: 0, live_stats: null }]}
        latest={{ status: 'provisioning', progress_pct: 0, live_stats: null }}
      />,
    )
    for (const key of ['vusers', 'tps', 'error_rate', 'p95_ms']) {
      expect(screen.getByTestId(`engine-pill-${key}`)).toHaveTextContent('—')
    }
    expect(screen.getByTestId('engine-status')).toHaveTextContent('provisioning · 0%')
  })

  it('switches the charted series via the metric tabs', async () => {
    const user = userEvent.setup()
    const samples = engineSamples(5)
    render(<EngineStrip samples={samples} latest={samples[4]} />)

    const tps = screen.getByRole('tab', { name: 'TPS' })
    const p95 = screen.getByRole('tab', { name: 'p95' })
    expect(tps).toHaveAttribute('aria-selected', 'true')

    await user.click(p95)
    expect(p95).toHaveAttribute('aria-selected', 'true')
    expect(tps).toHaveAttribute('aria-selected', 'false')
  })
})
