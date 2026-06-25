import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'

import type { PipelineDetail } from '@/api/hooks/useThreadState'
import { server } from '@/test/server'

import { PIPELINE_DETAIL, pipelineDetailHandler, renderRunRoutes, THREAD_ID } from './testUtils'

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

vi.mock('@/streaming/usePipelineStream', () => ({
  useRunLiveness: () => ({
    runId: null,
    stream: {
      status: 'idle',
      phaseProgress: {},
      toolCalls: [],
      engineStats: { samples: [], latest: null },
      pendingGateHint: null,
    },
  }),
}))

function promptReviewHandlers(captured: { body?: unknown }) {
  return [
    http.get('*/v1/pipelines/:threadId/phases/:phase/prompt-review', ({ params }) =>
      HttpResponse.json({
        system: `Server system for ${params['phase']}`,
        phase_prompt: `Server phase prompt for ${params['phase']}`,
        application: 'Server application prompt.',
        additional_context: '',
        source: { origin: 'catalog', ref: `phase/${params['phase']}@test` },
        updated_at: '2026-06-01T00:00:00+00:00',
        updated_by: 'system',
      }),
    ),
    http.patch('*/v1/pipelines/:threadId/phases/:phase/prompt-review', async ({ request }) => {
      captured.body = await request.json()
      return HttpResponse.json({
        ...(captured.body as Record<string, unknown>),
        source: { origin: 'run_override', ref: 'phase/story_analysis@test', editor: 'op' },
        updated_at: '2026-06-01T00:01:00+00:00',
        updated_by: 'op',
      })
    }),
  ]
}

describe('PromptReviewSection', () => {
  it('renders above workspace tabs and saves the full run-scoped draft', async () => {
    const captured: { body?: unknown } = {}
    server.use(pipelineDetailHandler(), ...promptReviewHandlers(captured))
    const user = userEvent.setup()
    renderRunRoutes([`/runs/${THREAD_ID}/phases/story_analysis?tab=details`])

    const section = await screen.findByTestId('prompt-review-section')
    const workspaceTabs = screen.getByRole('tablist', { name: 'Phase workspace tabs' })
    expect(
      section.compareDocumentPosition(workspaceTabs) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()

    const systemEditor = await within(section).findByLabelText('System Prompt')
    await waitFor(() => expect(systemEditor).toHaveValue('Server system for story_analysis'))

    await user.click(within(section).getByRole('tab', { name: 'Phase Prompt' }))
    const phaseEditor = within(section).getByLabelText('Phase Prompt')
    await user.clear(phaseEditor)
    await user.type(phaseEditor, 'Edited phase prompt.')

    await user.click(within(section).getByRole('tab', { name: 'Additional Context' }))
    const contextEditor = within(section).getByLabelText('Additional Context')
    await user.type(contextEditor, 'Extra operator context.')

    await user.click(within(section).getByRole('button', { name: 'Save to run' }))

    await waitFor(() =>
      expect(captured.body).toEqual({
        system: 'Server system for story_analysis',
        phase_prompt: 'Edited phase prompt.',
        application: 'Server application prompt.',
        additional_context: 'Extra operator context.',
      }),
    )
    expect(await within(section).findByText('saved')).toBeInTheDocument()
  })

  it('suppresses the standalone section while a prompt-review gate owns the editor', async () => {
    const captured: { body?: unknown } = {}
    const gated: PipelineDetail = {
      ...PIPELINE_DETAIL,
      thread_status: 'interrupted',
      current_phase: 'test_planning',
      pending_gate: { interrupt_id: 'int-prompt', kind: 'prompt_review', phase: 'test_planning' },
      interrupts: [
        {
          interrupt_id: 'int-prompt',
          kind: 'prompt_review',
          phase: 'test_planning',
          payload: {
            schema_version: 1,
            kind: 'prompt_review',
            phase: 'test_planning',
            prompt: {
              system: 'Gate system.',
              user: 'Gate phase prompt.',
              application: 'Gate app prompt.',
              source: { origin: 'catalog', ref: 'phase/test_planning@v2' },
            },
            additional_context: 'Gate context.',
            context_packets: [],
            tools: [],
            editable: true,
            actions: ['approve', 'modify', 'skip_phase', 'abort'],
          },
        },
      ],
    }
    server.use(pipelineDetailHandler(gated), ...promptReviewHandlers(captured))
    renderRunRoutes([`/runs/${THREAD_ID}/phases/test_planning?tab=details`])

    expect(await screen.findByTestId('prompt-review-panel')).toBeInTheDocument()
    expect(screen.queryByTestId('prompt-review-section')).not.toBeInTheDocument()
    expect(screen.getAllByRole('tablist', { name: 'Prompt Review tabs' })).toHaveLength(1)
  })

  it('labels the Application Prompt as app-wide and links to the application catalog', async () => {
    server.use(pipelineDetailHandler(), ...promptReviewHandlers({}))
    const user = userEvent.setup()
    renderRunRoutes([`/runs/${THREAD_ID}/phases/story_analysis?tab=details`])

    const section = await screen.findByTestId('prompt-review-section')
    await user.click(within(section).getByRole('tab', { name: 'Application Prompt' }))

    expect(within(section).getByTestId('prompt-tab-note-application')).toBeInTheDocument()
    // The tab-aware catalog quick-link targets the application namespace, not phase/system.
    expect(within(section).getByRole('link', { name: 'Catalog' })).toHaveAttribute(
      'href',
      expect.stringContaining('/prompts/application'),
    )
  })
})
