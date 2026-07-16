/**
 * Launch path: the tabbed wizard builds the EXACT configurable (snapshotted),
 * creates thread + run via the SDK with D2's stream options, deletes the draft
 * best-effort, and navigates to /runs/{threadId}?tab=log.
 */
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'

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
  beforeEach(() => {
    window.sessionStorage.clear()
  })

  it('launches with the exact configurable, deletes the draft, and navigates', async () => {
    assistantsSearch.mockResolvedValue([])
    const launchBodies: Record<string, unknown>[] = []
    server.use(
      http.post('*/v1/pipelines', async ({ request }) => {
        launchBodies.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json(
          {
            thread_id: 'thread-new',
            run_id: 'run-1',
            stream_url: '/threads/thread-new/runs/run-1/stream',
          },
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
          connection_id: 'conn-1',
          provider: 'jira',
        }),
      ),
      http.post('*/v1/pipelines', async ({ request }) => {
        bodies.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json(
          {
            thread_id: 'thread-context',
            run_id: 'run-context',
            stream_url: '/threads/thread-context/runs/run-context/stream',
          },
          { status: 202 },
        )
      }),
    )
    const draft = emptyDraft()
    draft.title = 'Context run'
    draft.request = 'Use the linked evidence'
    draft.work_items = [
      { key: 'PHX-241', connection_id: 'conn-1', provider: 'jira' },
    ]
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
          id: 'workitem-1',
          source: 'work_tracking',
          title: 'Checkout retries',
          summary: 'story · open',
          ref: 'https://tracker.example/PHX-241',
          text: 'Retry checkout without duplicate charges.',
        },
      ],
    })
  })

  it('replays the same resolved work-item packet after an ambiguous launch', async () => {
    const keys: string[] = []
    const descriptions: string[] = []
    let workItemReads = 0
    server.use(
      http.get('*/v1/work-tracking/items/PHX-241', () => {
        workItemReads += 1
        return HttpResponse.json({
          key: 'PHX-241',
          title: 'Checkout retries',
          kind: 'story',
          status: 'open',
          description:
            workItemReads === 1
              ? 'Original ticket description.'
              : 'Ticket edited after the first launch.',
          url: 'https://tracker.example/PHX-241',
          connection_id: 'conn-jira',
          provider: 'jira',
        })
      }),
      http.post('*/v1/pipelines', async ({ request }) => {
        const body = (await request.json()) as {
          idempotency_key: string
          context_packets?: Array<{ text?: string }>
        }
        keys.push(body.idempotency_key)
        descriptions.push(body.context_packets?.[0]?.text ?? '')
        return keys.length === 1
          ? HttpResponse.json({ detail: 'ambiguous outage' }, { status: 503 })
          : HttpResponse.json(
              {
                thread_id: 'thread-context-retry',
                run_id: 'run-context-retry',
                stream_url: '/threads/thread-context-retry/runs/run-context-retry/stream',
              },
              { status: 202 },
            )
      }),
    )
    const draft = emptyDraft()
    draft.title = 'Context retry'
    draft.request = 'Launch from the linked ticket'
    draft.work_items = [
      { key: 'PHX-241', connection_id: 'conn-jira', provider: 'jira' },
    ]

    await expect(launchWizardRun(draft)).rejects.toThrow('ambiguous outage')
    await expect(launchWizardRun(draft)).resolves.toEqual({
      threadId: 'thread-context-retry',
      runId: 'run-context-retry',
    })

    expect(keys[1]).toBe(keys[0])
    expect(descriptions).toEqual([
      'Original ticket description.',
      'Original ticket description.',
    ])
    expect(workItemReads).toBe(1)
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

  it('reuses the original key when a failed wizard payload is edited and reverted', async () => {
    assistantsSearch.mockResolvedValue([])
    const keys: string[] = []
    server.use(
      http.post('*/v1/pipelines', async ({ request }) => {
        const body = (await request.json()) as { idempotency_key: string }
        keys.push(body.idempotency_key)
        return keys.length === 1
          ? HttpResponse.json({ detail: 'ambiguous outage' }, { status: 503 })
          : HttpResponse.json(
              {
                thread_id: 'thread-reverted',
                run_id: 'run-reverted',
                stream_url: '/threads/thread-reverted/runs/run-reverted/stream',
              },
              { status: 202 },
            )
      }),
    )
    installWizardHandlers()
    const user = userEvent.setup()
    const { router } = renderWizard()

    await fillScope(user, screen)
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await user.click(screen.getByRole('button', { name: 'Launch Pipeline' }))
    await screen.findByText('Launch failed: ambiguous outage')

    await user.click(screen.getByRole('tab', { name: 'Scope' }))
    const request = screen.getByLabelText('Request')
    await user.type(request, ' changed')
    await user.clear(request)
    await user.type(request, 'Soak the checkout flow for 1h')
    await user.click(screen.getByRole('tab', { name: 'Review' }))
    await user.click(screen.getByRole('button', { name: 'Launch Pipeline' }))

    await waitFor(() => expect(keys).toHaveLength(2))
    expect(keys[1]).toBe(keys[0])
    await waitFor(() => expect(router.state.location.pathname).toBe('/runs/thread-reverted'))
  })

  it('reuses an ambiguous key after restoring the same wizard payload', async () => {
    const keys: string[] = []
    server.use(
      http.post('*/v1/pipelines', async ({ request }) => {
        const body = (await request.json()) as { idempotency_key: string }
        keys.push(body.idempotency_key)
        return keys.length === 1
          ? HttpResponse.json({ detail: 'temporary outage' }, { status: 503 })
          : HttpResponse.json(
              {
                thread_id: 'thread-restored',
                run_id: 'run-restored',
                stream_url: '/threads/thread-restored/runs/run-restored/stream',
              },
              { status: 202 },
            )
      }),
    )
    const draft = emptyDraft()
    draft.title = 'Restored wizard run'
    draft.request = 'Retry the exact saved launch'

    await expect(launchWizardRun(draft)).rejects.toThrow('temporary outage')
    await expect(
      launchWizardRun({
        ...draft,
        // A restored server draft may carry a different legacy component key;
        // canonical request identity remains the source of truth.
        launch_idempotency_key: 'legacy-restored-key',
      }),
    ).resolves.toEqual({
      threadId: 'thread-restored',
      runId: 'run-restored',
    })

    expect(keys).toHaveLength(2)
    expect(keys[1]).toBe(keys[0])
  })
})
