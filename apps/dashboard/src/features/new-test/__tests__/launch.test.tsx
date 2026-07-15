/**
 * Launch path: the tabbed wizard builds the EXACT configurable (snapshotted),
 * creates thread + run via the SDK with D2's stream options, deletes the draft
 * best-effort, and navigates to /runs/{threadId}?tab=log.
 */
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'

import { server } from '@/test/server'

import { launchWizardRun } from '../useWizardLaunch'
import { emptyDraft } from '../wizardState'
import { fillScope, installWizardHandlers, renderWizard } from './wizardTestUtils'

const { assistantsSearch } = vi.hoisted(() => ({
  assistantsSearch: vi.fn(),
}))

vi.mock('@/api/langgraphClient', () => ({
  getLangGraphClient: () =>
    Promise.resolve({
      assistants: { search: assistantsSearch },
    }),
}))

describe('wizard launch', () => {
  it('launches with the exact configurable, deletes the draft, and navigates', async () => {
    assistantsSearch.mockResolvedValue([])
    const launchBodies: Record<string, unknown>[] = []
    server.use(
      http.post('*/v1/pipelines', async ({ request }) => {
        launchBodies.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json(
          { thread_id: 'thread-new', run_id: 'run-1', stream_url: '/runs/run-1/stream' },
          { status: 202 },
        )
      }),
    )
    const { captured } = installWizardHandlers()
    const user = userEvent.setup()
    const { router } = renderWizard()

    // Scope -> save the draft explicitly (footer ghost button, no debounce wait).
    await fillScope(user, screen)
    await user.click(screen.getByRole('button', { name: 'Save Draft' }))
    await waitFor(() => expect(router.state.location.search).toContain('draft=draft-1'))

    // The review JSON is the exact payload the launch sends.
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    const json = JSON.parse(
      screen.getByTestId('launch-payload-json').textContent ?? '{}',
    ) as Record<string, unknown>

    await user.click(screen.getByRole('button', { name: 'Launch Pipeline' }))

    await waitFor(() => expect(launchBodies).toHaveLength(1))
    expect(launchBodies[0]).toEqual({
      assistant_id: json['assistant_id'],
      title: (json['input'] as Record<string, unknown>)['title'],
      request: (json['input'] as Record<string, unknown>)['request'],
      project_id: 'demo',
      idempotency_key: expect.any(String),
      configurable: json['configurable'],
    })

    // Snapshot the exact configurable contract sent to the backend.
    expect(json).toMatchInlineSnapshot(`
      {
        "assistant_id": "pipeline",
        "configurable": {
          "engine": "sim",
          "gates": {
            "env_triage": {
              "output_review": "gated",
              "prompt_review": "gated",
            },
            "execution": {
              "output_review": "gated",
              "prompt_review": "gated",
            },
            "postmortem": {
              "output_review": "gated",
              "prompt_review": "gated",
            },
            "reporting": {
              "output_review": "gated",
              "prompt_review": "gated",
            },
            "script_scenario": {
              "output_review": "gated",
              "prompt_review": "gated",
            },
            "story_analysis": {
              "output_review": "gated",
              "prompt_review": "gated",
            },
            "test_planning": {
              "output_review": "gated",
              "prompt_review": "gated",
            },
          },
          "project_id": "demo",
        },
        "document_ids": [],
        "input": {
          "request": "Soak the checkout flow for 1h",
          "title": "Checkout soak",
        },
        "metadata": {
          "project_id": "demo",
          "title": "Checkout soak",
        },
        "work_item_keys": [],
      }
    `)

    // Best-effort draft delete + navigation to the log tab.
    await waitFor(() => expect(captured.deletes).toEqual(['draft-1']))
    await waitFor(() => expect(router.state.location.pathname).toBe('/runs/thread-new'))
    expect(router.state.location.search).toBe('?tab=log')
    expect(screen.getByTestId('run-page')).toBeInTheDocument()
  })

  it('resolves work items and sends document ids with the selected assistant and full config', async () => {
    const bodies: Record<string, unknown>[] = []
    server.use(
      http.get('*/v1/work-tracking/items/PHX-241', () =>
        HttpResponse.json({
          key: 'PHX-241',
          title: 'Checkout retries',
          kind: 'story',
          status: 'open',
          description: 'Retry checkout without duplicate charges.',
          url: 'https://tracker.example/PHX-241',
        }),
      ),
      http.post('*/v1/pipelines', async ({ request }) => {
        bodies.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json(
          { thread_id: 'thread-context', run_id: 'run-context', stream_url: '/runs/x/stream' },
          { status: 202 },
        )
      }),
    )
    const draft = emptyDraft()
    draft.title = 'Context run'
    draft.request = 'Use the linked evidence'
    draft.work_item_keys = ['PHX-241']
    draft.document_ids = ['doc-9']
    draft.config.golden_config_id = 'asst-gold'
    draft.config.golden_configurable = {
      connections: { work_tracking: 'conn-1' },
      limits: { max_revise_loops: 7 },
    }

    await expect(launchWizardRun(draft)).resolves.toEqual({
      threadId: 'thread-context',
      runId: 'run-context',
    })
    expect(bodies[0]).toMatchObject({
      assistant_id: 'asst-gold',
      document_ids: ['doc-9'],
      configurable: {
        connections: { work_tracking: 'conn-1' },
        limits: { max_revise_loops: 7 },
      },
      context_packets: [
        {
          id: 'workitem-PHX-241',
          source: 'work_tracking',
          title: 'Checkout retries',
          summary: 'story · open',
          ref: 'https://tracker.example/PHX-241',
          text: 'Retry checkout without duplicate charges.',
        },
      ],
    })
  })

  it('stays on review with an inline error when the launch fails', async () => {
    assistantsSearch.mockResolvedValue([])
    server.use(
      http.post('*/v1/pipelines', () =>
        HttpResponse.json({ detail: 'multitask reject' }, { status: 409 }),
      ),
    )
    installWizardHandlers()
    const user = userEvent.setup()
    const { router, unmount } = renderWizard()

    await fillScope(user, screen)
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await user.click(screen.getByRole('button', { name: 'Launch Pipeline' }))

    expect(await screen.findByText('Launch failed: multitask reject')).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/runs/new')
    expect(screen.getByRole('tab', { name: 'Review' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('button', { name: 'Launch Pipeline' })).toBeEnabled() // retryable
    await user.click(screen.getByRole('button', { name: 'Save Draft' }))
    await screen.findByText('Draft saved')
    unmount()
  })
})
