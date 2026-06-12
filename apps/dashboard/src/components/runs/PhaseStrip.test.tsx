import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { PHASE_NAMES } from '@apex/pipeline-events'

import { PhaseStrip, type PhaseStripSegment } from './PhaseStrip'

const STRIP: PhaseStripSegment[] = [
  { phase: 'story_analysis', status: 'succeeded', attempt: 1 },
  { phase: 'test_planning', status: 'awaiting_prompt_review', attempt: 1 },
  { phase: 'env_triage', status: 'skipped', attempt: null },
  { phase: 'script_scenario', status: 'failed', attempt: 3 },
  { phase: 'execution', status: 'running', attempt: 2 },
  { phase: 'reporting', status: 'pending' },
  // postmortem intentionally absent -> "none" segment
]

describe('PhaseStrip', () => {
  it('renders 7 segments in canonical order with status-token classes', () => {
    render(<PhaseStrip strip={STRIP} />)

    const group = screen.getByRole('group', { name: 'Phase progress' })
    const segments = Array.from(group.children)
    expect(segments).toHaveLength(7)

    // canonical order regardless of input order
    segments.forEach((segment, i) => {
      expect(segment.getAttribute('aria-label')).toContain(PHASE_NAMES[i] ?? '')
    })

    expect(segments[0]).toHaveClass('phase-seg--succeeded')
    expect(segments[1]).toHaveClass('phase-seg--awaiting')
    expect(segments[2]).toHaveClass('phase-seg--skipped')
    expect(segments[3]).toHaveClass('phase-seg--failed')
    expect(segments[4]).toHaveClass('phase-seg--running')
    expect(segments[5]).toHaveClass('phase-seg--pending')
    expect(segments[6]).toHaveClass('phase-seg--none')
  })

  it('labels segments as "phase — status (attempt N)"', () => {
    render(<PhaseStrip strip={STRIP} />)

    const segment = screen.getByLabelText('execution — running (attempt 2)')
    expect(segment).toHaveAttribute('title', 'execution — running (attempt 2)')
    // no attempt -> suffix omitted
    expect(screen.getByLabelText('reporting — pending')).toBeInTheDocument()
  })

  it('renders focusable buttons and invokes onSelect with the phase when interactive', async () => {
    const onSelect = vi.fn()
    const user = userEvent.setup()
    render(<PhaseStrip strip={STRIP} onSelect={onSelect} />)

    const group = screen.getByRole('group', { name: 'Phase progress' })
    const buttons = within(group).getAllByRole('button')
    expect(buttons).toHaveLength(7)

    await user.click(screen.getByRole('button', { name: 'execution — running (attempt 2)' }))
    expect(onSelect).toHaveBeenCalledWith('execution')

    // keyboard: buttons are natively focusable + Enter-activatable
    const first = buttons[0]
    first?.focus()
    expect(first).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(onSelect).toHaveBeenCalledWith('story_analysis')
  })

  it('renders non-interactive spans (no buttons) without onSelect', () => {
    render(<PhaseStrip strip={STRIP} />)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('applies the size modifier class', () => {
    render(<PhaseStrip strip={STRIP} size="sm" />)
    expect(screen.getByRole('group', { name: 'Phase progress' })).toHaveClass('phase-strip--sm')
  })
})
