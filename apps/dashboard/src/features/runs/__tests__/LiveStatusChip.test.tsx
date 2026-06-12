import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { LiveStatusChip } from '../LiveStatusChip'

describe('LiveStatusChip', () => {
  it.each([
    ['idle', 'idle'],
    ['connecting', 'connecting'],
    ['live', 'live'],
    ['reconnecting', 'reconnecting'],
    ['ended', 'stream ended'],
    ['error', 'stream error'],
  ])('renders the %s state with tone class and explanatory title', (status, label) => {
    render(<LiveStatusChip status={status} />)
    const chip = screen.getByTestId('live-status-chip')
    expect(chip).toHaveTextContent(label)
    expect(chip).toHaveClass(status)
    expect(chip.getAttribute('title')).toBeTruthy()
  })

  it('renders unknown statuses muted with the raw text', () => {
    render(<LiveStatusChip status="warp-speed" />)
    const chip = screen.getByTestId('live-status-chip')
    expect(chip).toHaveTextContent('warp-speed')
    expect(chip).toHaveClass('idle') // muted fallback tone
    expect(chip).toHaveAttribute('title', 'Stream status: warp-speed')
  })
})
