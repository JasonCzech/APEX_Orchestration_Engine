import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { ActivityFeed } from '../ActivityFeed'

import { engineSamples, toolCall } from './liveFixtures'

/** jsdom has no layout: pin the scroll metrics the stick logic reads. */
function mockScrollMetrics(el: HTMLElement, { scrollHeight = 1000, clientHeight = 300 } = {}) {
  Object.defineProperty(el, 'scrollHeight', { value: scrollHeight, configurable: true })
  Object.defineProperty(el, 'clientHeight', { value: clientHeight, configurable: true })
}

describe('ActivityFeed', () => {
  it('renders phase_status dividers and tool-call cards from a scripted view', () => {
    const { rerender } = render(
      <ActivityFeed
        phase="execution"
        streamStatus="live"
        progress={{ status: 'running', attempt: 1 }}
        toolCalls={[
          toolCall('t1', 'engine.start', 'ok', 'execution', '2026-06-01T10:05:01+00:00'),
          toolCall('t2', 'engine.poll', 'error', 'execution'),
          toolCall('t3', 'work_tracking.search', 'ok', 'story_analysis'), // other phase: filtered
        ]}
      />,
    )

    const dividers = screen.getAllByTestId('activity-divider')
    expect(dividers).toHaveLength(1)
    expect(dividers[0]).toHaveTextContent('running')

    const cards = screen.getAllByTestId('activity-tool-card')
    expect(cards).toHaveLength(2)
    expect(cards[0]).toHaveTextContent('engine.start')
    expect(cards[0]?.querySelector('.status-badge')).toHaveClass('success')
    expect(cards[1]).toHaveTextContent('engine.poll')
    expect(cards[1]?.querySelector('.status-badge')).toHaveClass('danger')

    // Status transition -> a second divider appears; tool cards stay deduped.
    rerender(
      <ActivityFeed
        phase="execution"
        streamStatus="live"
        progress={{ status: 'succeeded', attempt: 1 }}
        toolCalls={[
          toolCall('t1', 'engine.start', 'ok', 'execution', '2026-06-01T10:05:01+00:00'),
          toolCall('t2', 'engine.poll', 'error', 'execution'),
        ]}
      />,
    )
    const after = screen.getAllByTestId('activity-divider')
    expect(after).toHaveLength(2)
    expect(after[1]).toHaveTextContent('succeeded')
    expect(screen.getAllByTestId('activity-tool-card')).toHaveLength(2)
  })

  it('renders real-agent response and error events for the selected phase', () => {
    render(
      <ActivityFeed
        phase="story_analysis"
        streamStatus="live"
        agentEvents={[
          {
            type: 'agent_message',
            phase: 'story_analysis',
            model: 'claude-sonnet-4-5',
            chars: 842,
          },
          {
            type: 'agent_error',
            phase: 'story_analysis',
            error: 'provider request timed out',
          },
          {
            type: 'agent_error',
            phase: 'reporting',
            error: 'other phase',
          },
        ]}
      />,
    )

    const cards = screen.getAllByTestId('activity-agent-card')
    expect(cards).toHaveLength(2)
    expect(cards[0]).toHaveTextContent('claude-sonnet-4-5 produced 842 characters')
    expect(cards[0]?.querySelector('.status-badge')).toHaveClass('success')
    expect(cards[1]).toHaveTextContent('provider request timed out')
    expect(cards[1]?.querySelector('.status-badge')).toHaveClass('danger')
  })

  it('renders retryable engine operation failures as warning telemetry', () => {
    render(
      <ActivityFeed
        phase="execution"
        streamStatus="live"
        engineErrors={[
          {
            type: 'engine_poll_error',
            phase: 'execution',
            attempt: 2,
            error: 'provider status request timed out',
            consecutive_errors: 3,
          },
          {
            type: 'engine_collection_settle_error',
            phase: 'execution',
            attempt: 2,
            error: 'provider teardown was unavailable',
            failure: 2,
            external_run_id: 'sim-1',
          },
        ]}
      />,
    )

    const cards = screen.getAllByTestId('activity-engine-error-card')
    expect(cards).toHaveLength(2)
    expect(cards[0]).toHaveTextContent('provider status request timed out')
    expect(cards[0]).toHaveTextContent('consecutive failure 3')
    expect(cards[1]).toHaveTextContent('provider teardown was unavailable')
    expect(cards[1]).toHaveTextContent('failure 2')
    expect(cards[1]?.querySelector('.status-badge')).toHaveClass('warning')
  })

  it('summarizes engine_poll ticks one expandable row per 10', () => {
    render(
      <ActivityFeed
        phase="execution"
        streamStatus="live"
        progress={{ status: 'running', attempt: 1 }}
        engineSamples={engineSamples(25)}
      />,
    )

    const rows = screen.getAllByTestId('activity-engine-row')
    expect(rows).toHaveLength(2) // 25 ticks -> rows for 1–10 and 11–20; 5 pending
    expect(rows[0]).toHaveTextContent('ticks 1–10')
    expect(rows[1]).toHaveTextContent('ticks 11–20')

    // Expandable: the details body lists the individual samples.
    expect(rows[0]?.querySelectorAll('.activity-engine-samples li')).toHaveLength(10)
  })

  it('sticks to the bottom and offers a jump-to-live pill when scrolled up', async () => {
    const user = userEvent.setup()
    const { rerender } = render(
      <ActivityFeed
        phase="execution"
        streamStatus="live"
        progress={{ status: 'running', attempt: 1 }}
        toolCalls={[toolCall('t1', 'engine.start')]}
      />,
    )

    const feed = screen.getByTestId('activity-feed')
    mockScrollMetrics(feed)

    // Operator scrolls away from the bottom -> pill appears, autoscroll stops.
    feed.scrollTop = 100
    fireEvent.scroll(feed)
    const pill = await screen.findByRole('button', { name: /jump to live/i })
    expect(pill).toBeInTheDocument()

    rerender(
      <ActivityFeed
        phase="execution"
        streamStatus="live"
        progress={{ status: 'running', attempt: 1 }}
        toolCalls={[toolCall('t1', 'engine.start'), toolCall('t2', 'engine.poll')]}
      />,
    )
    expect(feed.scrollTop).toBe(100) // not yanked while reading scrollback

    // Jump to live: scrolls to the end and re-sticks.
    await user.click(pill)
    expect(feed.scrollTop).toBe(1000)
    expect(screen.queryByRole('button', { name: /jump to live/i })).not.toBeInTheDocument()

    // Stuck again: new entries auto-scroll.
    fireEvent.scroll(feed) // scrollTop 1000 -> within stick threshold
    rerender(
      <ActivityFeed
        phase="execution"
        streamStatus="live"
        progress={{ status: 'running', attempt: 1 }}
        toolCalls={[
          toolCall('t1', 'engine.start'),
          toolCall('t2', 'engine.poll'),
          toolCall('t3', 'engine.collect'),
        ]}
      />,
    )
    expect(feed.scrollTop).toBe(1000)
  })

  it('caps rendered entries at 500 with a truncation notice', () => {
    const calls = Array.from({ length: 510 }, (_, index) => toolCall(`t${index}`, `tool_${index}`))
    render(<ActivityFeed phase="execution" streamStatus="live" toolCalls={calls} />)

    expect(screen.getAllByTestId('activity-tool-card')).toHaveLength(500)
    expect(screen.getByTestId('activity-truncated')).toHaveTextContent('10 older entries truncated')
  })

  it('renders the empty state until events arrive', () => {
    render(<ActivityFeed phase="reporting" streamStatus="idle" />)
    expect(screen.getByText('No live activity for this phase yet.')).toBeInTheDocument()
  })
})
