/**
 * phase_review flow through the real machine: summary/warnings/artifacts/
 * dialogue render; discuss sends {action:'discuss', message}; revise reveals
 * the inline instructions textarea and sends {action:'revise', instructions};
 * an accepted discuss parks the module in the awaiting-agent banner.
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import { GateModuleView } from '@/hitl/GateModule'
import { useGate } from '@/hitl/useGate'
import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { gatedDetail, mutableDetailHandler, phaseInterrupt, resumeHandler } from './gateFixtures'

vi.mock('@uiw/react-codemirror', async () => {
  const { createElement } = await import('react')
  return {
    default: ({ value }: { value: string }) =>
      createElement('pre', { 'data-testid': 'codemirror' }, value),
  }
})

function Harness({ threadId }: { threadId: string }) {
  const hitl = useGate(threadId)
  if (!hitl.gate) return <div data-testid="no-gate" />
  return (
    <GateModuleView
      threadId={threadId}
      gate={hitl.gate}
      machineState={hitl.state}
      onEdit={hitl.edit}
      onSubmit={hitl.submit}
      onViewCurrent={hitl.viewCurrent}
    />
  )
}

function renderHarness(threadId: string) {
  return render(
    <QueryClientProvider client={createTestQueryClient()}>
      <MemoryRouter>
        <Harness threadId={threadId} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('GateModule phase_review', () => {
  it('renders the review surfaces and discuss sends the composer message', async () => {
    const threadId = 'th-discuss'
    const { handler } = mutableDetailHandler(threadId, gatedDetail(threadId, [phaseInterrupt('int-d')]))
    const resume = resumeHandler(202)
    server.use(handler, resume.handler)
    const user = userEvent.setup()
    renderHarness(threadId)

    // Summary, warning card, artifact chip (linked into the run), dialogue tail.
    expect(await screen.findByTestId('gate-summary')).toHaveTextContent(
      'Planned 4 scenarios against staging.',
    )
    expect(screen.getByTestId('gate-warning')).toHaveTextContent('Latency budget is tight')
    expect(screen.getByRole('link', { name: /plan\.md/ })).toHaveAttribute(
      'href',
      `/runs/${threadId}/artifacts/art-plan`,
    )
    expect(screen.getByTestId('gate-dialogue')).toHaveTextContent('Tighten the ramp.')
    expect(screen.getByTestId('gate-dialogue')).toHaveTextContent('Ramp tightened to 5m.')

    // Discuss is disabled until the composer carries a message.
    const discuss = screen.getByRole('button', { name: 'Discuss' })
    expect(discuss).toBeDisabled()
    fireEvent.change(screen.getByPlaceholderText(/Ask a question or give feedback/), {
      target: { value: 'Why no auth flows in the plan?' },
    })
    expect(discuss).toBeEnabled()

    await user.click(discuss)
    await waitFor(() =>
      expect(resume.captured.last()).toEqual({
        threadId,
        interruptId: 'int-d',
        body: { action: 'discuss', message: 'Why no auth flows in the plan?' },
      }),
    )
    // Accepted discuss -> awaiting the agent (gate will reopen with a new id).
    expect(await screen.findByTestId('gate-awaiting')).toHaveTextContent(
      'Agent working on your message — the gate will reopen.',
    )
  })

  it('revise reveals the inline instructions textarea and sends them', async () => {
    const threadId = 'th-revise'
    const { handler } = mutableDetailHandler(threadId, gatedDetail(threadId, [phaseInterrupt('int-r')]))
    const resume = resumeHandler(202)
    server.use(handler, resume.handler)
    const user = userEvent.setup()
    renderHarness(threadId)

    await user.click(await screen.findByRole('button', { name: 'Revise…' }))
    const send = screen.getByRole('button', { name: 'Send revision' })
    expect(send).toBeDisabled()
    fireEvent.change(screen.getByPlaceholderText(/What should the agent change/), {
      target: { value: 'Add the auth flows and rebalance the mix.' },
    })
    expect(send).toBeEnabled()

    await user.click(send)
    await waitFor(() =>
      expect(resume.captured.last()).toEqual({
        threadId,
        interruptId: 'int-r',
        body: { action: 'revise', instructions: 'Add the auth flows and rebalance the mix.' },
      }),
    )
    expect(await screen.findByTestId('gate-awaiting')).toHaveTextContent(
      'Agent working on your revision instructions',
    )
  })
})
