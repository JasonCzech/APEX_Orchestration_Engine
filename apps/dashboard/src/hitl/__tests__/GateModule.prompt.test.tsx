/**
 * prompt_review flow through the REAL machine (useGate + GateModuleView wired
 * the way RunDetailPage mounts them): edit -> dirty chip -> Save Edit &
 * Re-review sends {action:'modify', prompt:{...}}; abort is gated by
 * type-to-confirm.
 */
import { useRef } from 'react'

import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import { GateModule, GateModuleView, type GateModuleHandle } from '@/hitl/GateModule'
import { useGate } from '@/hitl/useGate'
import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { gatedDetail, mutableDetailHandler, promptInterrupt, resumeHandler } from './gateFixtures'

// CodeMirror needs real DOM measurement; mock it as a controlled textarea that
// honors the editable/readOnly contract and forwards onChange(value).
vi.mock('@uiw/react-codemirror', async () => {
  const { createElement } = await import('react')
  return {
    default: ({
      value,
      onChange,
      editable,
      readOnly,
      'aria-label': ariaLabel,
    }: {
      value: string
      onChange?: (value: string) => void
      editable?: boolean
      readOnly?: boolean
      'aria-label'?: string
    }) =>
      createElement('textarea', {
        'data-testid': 'codemirror',
        'aria-label': ariaLabel,
        value,
        readOnly: readOnly === true || editable === false,
        onChange: (event: { target: { value: string } }) => onChange?.(event.target.value),
      }),
  }
})

/** RunDetailPage-shaped wiring: page-level machine + controlled view. */
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

function HandleHarness({
  threadId,
  interrupt,
}: {
  threadId: string
  interrupt: ReturnType<typeof promptInterrupt>
}) {
  const handle = useRef<GateModuleHandle | null>(null)
  return (
    <>
      <button type="button" onClick={() => handle.current?.invoke('abort')}>
        Invoke abort shortcut
      </button>
      <GateModule threadId={threadId} interrupt={interrupt} handleRef={handle} />
    </>
  )
}

describe('GateModule prompt_review', () => {
  it('edit -> dirty chip -> Save Edit & Re-review posts {action:"modify", prompt:{...}}', async () => {
    const threadId = 'th-prompt'
    const { handler, ref } = mutableDetailHandler(
      threadId,
      gatedDetail(threadId, [promptInterrupt('int-p')]),
    )
    const resume = resumeHandler(202)
    server.use(handler, resume.handler)
    const user = userEvent.setup()
    renderHarness(threadId)

    // Hydrated panel: provenance chip + editable editors seeded from payload.
    expect(await screen.findByTestId('gate-provenance')).toHaveTextContent(
      'catalog · test_planning@v2',
    )
    const panel = await screen.findByTestId('prompt-review-panel')
    let systemEditor = within(panel).getByLabelText('System Prompt')
    expect(systemEditor).toHaveValue('You are the planning agent.')
    await user.click(within(panel).getByRole('tab', { name: 'Application Prompt' }))
    let applicationEditor = within(panel).getByLabelText('Application Prompt')
    expect(applicationEditor).toHaveValue('Checkout must preserve carts during payment retries.')

    // Pristine: no dirty chip, base execute action is available.
    expect(screen.queryByTestId('gate-dirty-chip')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Execute Phase/i })).toBeEnabled()

    // Edit the system prompt -> dirty diff indicator + edited execute label.
    await user.click(within(panel).getByRole('tab', { name: 'System Prompt' }))
    systemEditor = within(panel).getByLabelText('System Prompt')
    fireEvent.change(systemEditor, { target: { value: 'You are the EDITED planning agent.' } })
    await user.click(within(panel).getByRole('tab', { name: 'Application Prompt' }))
    applicationEditor = within(panel).getByLabelText('Application Prompt')
    fireEvent.change(applicationEditor, {
      target: { value: 'Checkout must preserve carts and payment retry telemetry.' },
    })
    expect(await screen.findByTestId('gate-dirty-chip')).toHaveTextContent('edited')
    const modify = screen.getByRole('button', { name: /Save Edit & Re-review/i })
    expect(modify).toBeEnabled()

    const reviewed = promptInterrupt('int-p')
    if (reviewed.payload && typeof reviewed.payload === 'object') {
      const prompt = reviewed.payload['prompt'] as Record<string, unknown>
      prompt['system'] = 'You are the EDITED planning agent.'
      prompt['application'] = 'Checkout must preserve carts and payment retry telemetry.'
    }
    ref.current = gatedDetail(threadId, [reviewed])
    await user.click(modify)
    await waitFor(() =>
      expect(resume.captured.last()).toEqual({
        threadId,
        interruptId: 'int-p',
        body: {
          action: 'modify',
          prompt: {
            system: 'You are the EDITED planning agent.',
            user: 'Plan load coverage for APEX-101.',
            application: 'Checkout must preserve carts and payment retry telemetry.',
          },
        },
      }),
    )
    // Modify saves the draft and intentionally re-opens review after the
    // agent processes it. LangGraph reuses the interrupt id for this node.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Execute Phase/i })).toBeEnabled(),
    )
    await user.click(screen.getByRole('tab', { name: 'System Prompt' }))
    expect(screen.getByLabelText('System Prompt')).toHaveValue(
      'You are the EDITED planning agent.',
    )
  })

  it('renders context packets + tool chips, and abort requires typing ABORT', async () => {
    const threadId = 'th-abort'
    const { handler } = mutableDetailHandler(threadId, gatedDetail(threadId, [promptInterrupt('int-x')]))
    const resume = resumeHandler(202)
    server.use(handler, resume.handler)
    const user = userEvent.setup()
    renderHarness(threadId)

    const packets = await screen.findByTestId('gate-context-packets')
    expect(packets).toHaveTextContent('Additional Context')
    expect(packets).toHaveTextContent('APEX-101')
    expect(screen.getByTestId('gate-tools')).toHaveTextContent('jira.search')

    // Arm the abort: confirm stays disabled until ABORT is typed, case/space-insensitive.
    await user.click(screen.getByRole('button', { name: 'Abort' }))
    const confirm = screen.getByRole('button', { name: 'Confirm abort' })
    expect(confirm).toBeDisabled()
    const input = screen.getByLabelText('Type ABORT to confirm')
    await user.type(input, 'not abort')
    expect(confirm).toBeDisabled()
    await user.clear(input)
    await user.type(input, ' abort ')
    expect(confirm).toBeEnabled()
    expect(resume.captured.calls).toHaveLength(0) // nothing submitted yet

    await user.click(confirm)
    await waitFor(() =>
      expect(resume.captured.last()).toMatchObject({ body: { action: 'abort' } }),
    )
  })

  it('arms the typed confirmation instead of aborting through the imperative shortcut', async () => {
    const threadId = 'th-shortcut-abort'
    const interrupt = promptInterrupt('int-shortcut')
    const { handler } = mutableDetailHandler(threadId, gatedDetail(threadId, [interrupt]))
    const resume = resumeHandler(202)
    server.use(handler, resume.handler)
    const user = userEvent.setup()

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter>
          <HandleHarness threadId={threadId} interrupt={interrupt} />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await screen.findByTestId('gate-module')
    await user.click(screen.getByRole('button', { name: 'Invoke abort shortcut' }))

    expect(screen.getByLabelText('Type ABORT to confirm')).toHaveFocus()
    expect(screen.getByRole('button', { name: 'Confirm abort' })).toBeDisabled()
    expect(resume.captured.calls).toHaveLength(0)
  })
})
