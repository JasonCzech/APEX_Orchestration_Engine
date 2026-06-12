/**
 * The self-contained GateModule against the approvals-inbox contract
 * (features/approvals/gateModuleContract.ts): handleRef keyboard delegation
 * (invoke/isActionable) and the once-per-instance onOutcome notification
 * ('resumed' on 202, 'superseded' on 409 CAS loss).
 */
import { createRef } from 'react'

import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import { GateModule, type GateModuleHandle } from '@/hitl/GateModule'
import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { gatedDetail, mutableDetailHandler, promptInterrupt, resumeHandler } from './gateFixtures'

vi.mock('@uiw/react-codemirror', async () => {
  const { createElement } = await import('react')
  return {
    default: ({ value }: { value: string }) =>
      createElement('textarea', { 'data-testid': 'codemirror', value, onChange: () => undefined }),
  }
})

function renderContract(threadId: string, interruptId: string) {
  const handleRef = createRef<GateModuleHandle | null>()
  const onOutcome = vi.fn()
  render(
    <QueryClientProvider client={createTestQueryClient()}>
      <MemoryRouter>
        <GateModule
          threadId={threadId}
          interrupt={promptInterrupt(interruptId)}
          compact
          onOutcome={onOutcome}
          handleRef={handleRef}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { handleRef, onOutcome }
}

describe('GateModule (inbox contract)', () => {
  it('invoke("approve") submits through the machine and fires onOutcome resumed once', async () => {
    const threadId = 'th-contract-a'
    const { handler } = mutableDetailHandler(threadId, gatedDetail(threadId, [promptInterrupt('int-c1')]))
    const resume = resumeHandler(202)
    server.use(handler, resume.handler)
    const { handleRef, onOutcome } = renderContract(threadId, 'int-c1')

    await screen.findByTestId('gate-module')
    expect(handleRef.current?.isActionable()).toBe(true)
    // Unavailable actions are rejected without side effects.
    expect(handleRef.current?.invoke('discuss')).toBe(false)

    expect(handleRef.current?.invoke('approve')).toBe(true)
    await waitFor(() =>
      expect(onOutcome).toHaveBeenCalledWith({
        type: 'resumed',
        action: 'approve',
        runId: 'run-99',
      }),
    )
    expect(onOutcome).toHaveBeenCalledTimes(1)
    expect(resume.captured.last()).toMatchObject({
      interruptId: 'int-c1',
      body: { action: 'approve' },
    })
    // Terminal: not actionable anymore, repeated invokes no-op.
    expect(handleRef.current?.isActionable()).toBe(false)
    expect(handleRef.current?.invoke('approve')).toBe(false)
  })

  it('409 CAS loss fires onOutcome superseded and renders the banner', async () => {
    const threadId = 'th-contract-b'
    const { handler } = mutableDetailHandler(threadId, gatedDetail(threadId, [promptInterrupt('int-c2')]))
    server.use(handler, resumeHandler(409).handler)
    const { handleRef, onOutcome } = renderContract(threadId, 'int-c2')

    await screen.findByTestId('gate-module')
    handleRef.current?.invoke('approve')
    await waitFor(() => expect(onOutcome).toHaveBeenCalledWith({ type: 'superseded' }))
    expect(screen.getByTestId('gate-superseded')).toHaveTextContent(
      'Another operator resumed this gate',
    )
  })
})
