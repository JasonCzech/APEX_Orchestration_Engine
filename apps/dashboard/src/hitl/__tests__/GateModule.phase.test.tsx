/**
 * phase_review flow through the real machine: summary/warnings/artifacts/
 * dialogue render; Ask agent sends {action:'discuss', message}; Request
 * Re-run reveals the inline instructions textarea and sends
 * {action:'revise', instructions}; an accepted discuss parks the module in the
 * awaiting-agent banner.
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
  it('discloses omitted preview evidence while keeping the review actionable', async () => {
    const threadId = 'th-bounded-evidence'
    const interrupt = phaseInterrupt('int-bounded')
    interrupt.payload = { ...(interrupt.payload as object), evidence: { projection_truncated: true } }
    const { handler } = mutableDetailHandler(threadId, gatedDetail(threadId, [interrupt]))
    server.use(handler)
    renderHarness(threadId)
    expect(await screen.findByRole('note')).toHaveTextContent('Some agent evidence was omitted from this review preview')
    expect(screen.getByText('Reasoning details are unavailable in this preview.')).toBeInTheDocument()
    expect(screen.queryByText('No reasoning details recorded for this phase.')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Accept phase output & continue' })).toBeEnabled()
  })

  it('discloses bounded output and discussion while keeping the decision available', async () => {
    const threadId = 'th-bounded-output'
    const interrupt = phaseInterrupt('int-bounded-output')
    interrupt.payload = { ...(interrupt.payload as object), projection_truncated: true, summary: null }
    const { handler } = mutableDetailHandler(threadId, gatedDetail(threadId, [interrupt]))
    server.use(handler)
    renderHarness(threadId)
    expect(await screen.findByRole('note')).toHaveTextContent('Some output or discussion was omitted from this review preview')
    expect(screen.getByText('The phase summary is unavailable in this review preview.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Accept phase output & continue' })).toBeEnabled()
  })

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

    // Ask agent is disabled until the composer carries a message.
    const discuss = screen.getByRole('button', { name: 'Ask agent' })
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

    await user.click(await screen.findByRole('button', { name: /Request changes/i }))
    const send = screen.getByRole('button', { name: /Re-run with Changes/i })
    expect(send).toBeDisabled()
    expect(screen.getByPlaceholderText(/What should the agent change/)).toHaveFocus()
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
  it('shows agent evidence before accepting output and never colors unknown tool outcomes as success', async () => {
    const threadId = 'th-gate-evidence'
    const interrupt = phaseInterrupt('int-evidence')
    interrupt.payload = { ...(interrupt.payload as object), evidence: {
      attempt: 2, provenance: 'llm', reasoning_digest: 'Checked the actual service contract.',
      resolved_prompt: { system: 'Use governed tools.', user: 'Produce the workload.' },
      resolved_prompt_source: { origin: 'run_override', ref: 'phase/test_planning@v2', editor: 'Operator Ada' },
      tool_calls: [{ id: 'tool-timeout', tool: 'catalog.lookup', status: 'timeout' }, { id: 'tool-unknown', tool: 'catalog.future', status: 'new-state' }],
      transcript_ref: { id: 'pending-transcript', name: 'Evidence transcript' },
    } }
    const { handler } = mutableDetailHandler(threadId, gatedDetail(threadId, [interrupt]))
    server.use(handler)
    renderHarness(threadId)
    expect(await screen.findByText('Checked the actual service contract.')).toBeInTheDocument()
    expect(screen.getByText(/Edited for this run by Operator Ada/)).toBeInTheDocument()
    expect(screen.getByText('timeout')).toHaveClass('danger')
    expect(screen.getByText('new-state')).toHaveClass('neutral')
    expect(screen.getByRole('link', { name: 'Full agent transcript (attempt 2)' })).toHaveAttribute('href', `/runs/${threadId}/artifacts/pending-transcript`)
    expect(screen.getByRole('button', { name: 'Accept phase output & continue' })).toBeEnabled()
  })

})
