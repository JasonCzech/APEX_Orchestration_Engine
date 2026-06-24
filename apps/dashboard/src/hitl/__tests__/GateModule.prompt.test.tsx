/**
 * prompt_review flow through the REAL machine (useGate + GateModuleView wired
 * the way RunDetailPage mounts them): edit -> dirty chip -> Execute Edited
 * Prompt sends {action:'modify', prompt:{...}}; abort is gated by
 * type-to-confirm.
 */
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import { GateModuleView } from '@/hitl/GateModule'
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
    }: {
      value: string
      onChange?: (value: string) => void
      editable?: boolean
      readOnly?: boolean
    }) =>
      createElement('textarea', {
        'data-testid': 'codemirror',
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

describe('GateModule prompt_review', () => {
  it('edit -> dirty chip -> Execute Edited Prompt posts {action:"modify", prompt:{...}}', async () => {
    const threadId = 'th-prompt'
    const { handler } = mutableDetailHandler(threadId, gatedDetail(threadId, [promptInterrupt('int-p')]))
    const resume = resumeHandler(202)
    server.use(handler, resume.handler)
    const user = userEvent.setup()
    renderHarness(threadId)

    // Hydrated panel: provenance chip + editable editors seeded from payload.
    expect(await screen.findByTestId('gate-provenance')).toHaveTextContent(
      'catalog · test_planning@v2',
    )
    const systemEditor = within(screen.getByTestId('gate-editor-system')).getByTestId('codemirror')
    expect(systemEditor).toHaveValue('You are the planning agent.')

    // Pristine: no dirty chip, base execute action is available.
    expect(screen.queryByTestId('gate-dirty-chip')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Execute Phase/i })).toBeEnabled()

    // Edit the system prompt -> dirty diff indicator + edited execute label.
    fireEvent.change(systemEditor, { target: { value: 'You are the EDITED planning agent.' } })
    expect(await screen.findByTestId('gate-dirty-chip')).toHaveTextContent('edited')
    const modify = screen.getByRole('button', { name: /Execute Edited Prompt/i })
    expect(modify).toBeEnabled()

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
          },
        },
      }),
    )
    // 202 settles the gate: the module unmounts (stream/poll narrative next).
    await waitFor(() => expect(screen.getByTestId('no-gate')).toBeInTheDocument())
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
})
