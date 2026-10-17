import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { RunAnnouncements } from '../RunAnnouncements'

describe('Run announcements', () => {
  const phases = [
    { phase: 'story_analysis', status: 'succeeded' },
    { phase: 'test_planning', status: 'awaiting_prompt_review' },
    { phase: 'env_triage', status: 'pending' },
    { phase: 'script_scenario', status: 'pending' },
    { phase: 'execution', status: 'pending' },
    { phase: 'reporting', status: 'pending' },
    { phase: 'postmortem', status: 'pending' },
  ]

  it('keeps queue selection concise and does not announce unrelated phase or transport state', () => {
    const props = { threadId: 'checkout', title: 'Checkout run', phases, currentPhase: 'test_planning',
      gate: { kind: 'prompt_review', phase: 'test_planning' }, streamStatus: 'idle', terminal: false, triage: true,
      queuePosition: { position: 1, total: 2 } }
    const { rerender } = render(<RunAnnouncements {...props} />)
    expect(screen.getByRole('status')).toHaveTextContent('Checkout run. Review prompts open on Test Planning. Queue item 1 of 2.')
    expect(screen.getByRole('status')).not.toHaveTextContent('Story Analysis')
    expect(screen.getByRole('status')).not.toHaveTextContent('Snapshot refresh active')
    rerender(<RunAnnouncements {...props} threadId="payment" title="Payment run" queuePosition={{ position: 2, total: 2 }} />)
    expect(screen.getByRole('status').textContent).toBe('Payment run. Review prompts open on Test Planning. Queue item 2 of 2.')
  })

  it('announces only changed phases and transports while the same run stays selected', () => {
    const props = { threadId: 'checkout', title: 'Checkout run', phases, currentPhase: 'test_planning', streamStatus: 'live', terminal: false, triage: true }
    const { rerender } = render(<RunAnnouncements {...props} />)
    rerender(<RunAnnouncements {...props} gate={{ phase: 'test_planning', kind: 'phase_review' }}
      phases={phases.map(phase => phase.phase === 'test_planning' ? { ...phase, status: 'awaiting_output_review' } : phase)} streamStatus="reconnecting" />)
    expect(screen.getByRole('status')).toHaveTextContent('Review phase results open on Test Planning')
    expect(screen.getByRole('status')).toHaveTextContent('Test Planning: Results ready')
    expect(screen.getByRole('status')).toHaveTextContent('Reconnecting live updates')
    expect(screen.getByRole('status')).not.toHaveTextContent('Story Analysis')
    expect(screen.getByRole('status')).not.toHaveTextContent('Execution:')
    rerender(<RunAnnouncements {...props} terminal />)
    expect(screen.getByRole('status')).toHaveTextContent('Recorded snapshot')
    expect(screen.getByRole('status')).not.toHaveTextContent('Live updates connected')
  })

  it('announces a new gate instance and ignores unchanged snapshot objects', () => {
    const props = { threadId: 'checkout', title: 'Checkout run', phases, currentPhase: 'test_planning', streamStatus: 'idle', terminal: false,
      gate: { kind: 'prompt_review', phase: 'test_planning', interrupt_id: 'first' } }
    const { rerender } = render(<RunAnnouncements {...props} />)
    rerender(<RunAnnouncements {...props} gate={{ ...props.gate, interrupt_id: 'second' }} />)
    const announcement = 'Checkout run. Review prompts reopened on Test Planning.'
    expect(screen.getByRole('status').textContent).toBe(announcement)
    const region = screen.getByRole('status')
    const observer = new MutationObserver(() => undefined)
    observer.observe(region, { childList: true, subtree: true, characterData: true })
    rerender(<RunAnnouncements {...props} gate={{ ...props.gate, interrupt_id: 'second' }} phases={phases.map(phase => ({ ...phase }))} />)
    expect(screen.getByRole('status').textContent).toBe(announcement)
    expect(observer.takeRecords()).toHaveLength(0)
    rerender(<RunAnnouncements {...props} gate={{ ...props.gate, interrupt_id: 'third' }} />)
    expect(screen.getByRole('status')).toBe(region)
    expect(region.textContent).toBe(announcement)
    expect(observer.takeRecords().length).toBeGreaterThan(0)
    rerender(<RunAnnouncements {...props} gate={{ ...props.gate, interrupt_id: 'third' }} />)
    expect(observer.takeRecords()).toHaveLength(0)
    observer.disconnect()
  })

  it('announces successive canonical generations that reuse the same interrupt id', () => {
    const props = { threadId: 'checkout', title: 'Checkout run', phases, currentPhase: 'test_planning', streamStatus: 'idle', terminal: false,
      gate: { kind: 'prompt_review', phase: 'test_planning', interrupt_id: 'reused' } }
    const { rerender } = render(<RunAnnouncements {...props} gateGeneration="generation-1" />)
    const region = screen.getByRole('status')
    const observer = new MutationObserver(() => undefined)
    observer.observe(region, { childList: true, subtree: true, characterData: true })
    for (const generation of ['generation-2', 'generation-3']) {
      rerender(<RunAnnouncements {...props} gateGeneration={generation} />)
      expect(region.textContent).toBe('Checkout run. Review prompts updated on Test Planning.')
      expect(observer.takeRecords().length).toBeGreaterThan(0)
      rerender(<RunAnnouncements {...props} gate={{ ...props.gate }} phases={phases.map(phase => ({ ...phase }))} gateGeneration={generation} />)
      expect(observer.takeRecords()).toHaveLength(0)
    }
    observer.disconnect()
  })
})
