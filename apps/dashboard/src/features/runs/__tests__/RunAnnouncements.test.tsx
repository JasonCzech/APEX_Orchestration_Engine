import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { RunAnnouncements } from '../RunAnnouncements'

describe('Run announcements', () => {
  it('announces human-readable gates, phase changes and transport transitions together', () => {
    const props = { title: 'Checkout run', phases: [{ phase: 'test_planning', status: 'running' }], streamStatus: 'live', terminal: false }
    const { rerender } = render(<RunAnnouncements {...props} />)
    expect(screen.getByRole('status')).toHaveTextContent('Test Planning: Executing')
    rerender(<RunAnnouncements {...props} gate={{ phase: 'test_planning', kind: 'phase_review' }} phases={[{ phase: 'test_planning', status: 'awaiting_output_review' }]} streamStatus="reconnecting" />)
    expect(screen.getByRole('status')).toHaveTextContent('Review phase results open on Test Planning')
    expect(screen.getByRole('status')).toHaveTextContent('Reconnecting live updates')
    rerender(<RunAnnouncements {...props} terminal />)
    expect(screen.getByRole('status')).toHaveTextContent('Recorded snapshot')
    expect(screen.getByRole('status')).not.toHaveTextContent('Live updates connected')
  })
})
