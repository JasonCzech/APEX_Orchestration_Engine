/**
 * Launch path: the single-scroll wizard builds the EXACT configurable
 * (snapshotted), creates thread + run via the SDK with D2's stream options,
 * deletes the draft best-effort, and navigates to /runs/{threadId}?tab=log.
 */
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { fillScope, installWizardHandlers, renderWizard } from './wizardTestUtils'

const { assistantsSearch, threadsCreate, runsCreate } = vi.hoisted(() => ({
  assistantsSearch: vi.fn(),
  threadsCreate: vi.fn(),
  runsCreate: vi.fn(),
}))

vi.mock('@/api/langgraphClient', () => ({
  getLangGraphClient: () =>
    Promise.resolve({
      assistants: { search: assistantsSearch },
      threads: { create: threadsCreate },
      runs: { create: runsCreate },
    }),
}))

describe('wizard launch', () => {
  it('launches with the exact configurable, deletes the draft, and navigates', async () => {
    assistantsSearch.mockResolvedValue([])
    threadsCreate.mockResolvedValue({ thread_id: 'thread-new' })
    runsCreate.mockResolvedValue({ run_id: 'run-1' })
    const { captured } = installWizardHandlers()
    const user = userEvent.setup()
    const { router } = renderWizard()

    // Scope -> save the draft explicitly (footer ghost button, no debounce wait).
    await fillScope(user, screen)
    await user.click(screen.getByRole('button', { name: 'Save Draft' }))
    await waitFor(() => expect(router.state.location.search).toContain('draft=draft-1'))

    // The review JSON is the exact payload the launch sends.
    const json = JSON.parse(
      screen.getByTestId('launch-payload-json').textContent ?? '{}',
    ) as Record<string, unknown>

    await user.click(screen.getByRole('button', { name: 'Launch Pipeline' }))

    await waitFor(() => expect(runsCreate).toHaveBeenCalledTimes(1))
    expect(threadsCreate).toHaveBeenCalledWith({ metadata: json['metadata'] })
    expect(runsCreate).toHaveBeenCalledWith('thread-new', 'pipeline', {
      input: json['input'],
      config: { recursion_limit: expect.any(Number), configurable: json['configurable'] },
      streamMode: ['updates', 'messages-tuple', 'custom'],
      streamSubgraphs: true,
      streamResumable: true,
      durability: 'sync',
      multitaskStrategy: 'reject',
    })

    // Snapshot the exact configurable contract sent to the backend.
    expect(json).toMatchInlineSnapshot(`
      {
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
        "input": {
          "request": "Soak the checkout flow for 1h",
          "title": "Checkout soak",
        },
        "metadata": {
          "project_id": "demo",
          "title": "Checkout soak",
        },
      }
    `)

    // Best-effort draft delete + navigation to the log tab.
    await waitFor(() => expect(captured.deletes).toEqual(['draft-1']))
    await waitFor(() => expect(router.state.location.pathname).toBe('/runs/thread-new'))
    expect(router.state.location.search).toBe('?tab=log')
    expect(screen.getByTestId('run-page')).toBeInTheDocument()
  })

  it('stays on review with an inline error when the launch fails', async () => {
    assistantsSearch.mockResolvedValue([])
    threadsCreate.mockResolvedValue({ thread_id: 'thread-doomed' })
    runsCreate.mockRejectedValue(new Error('multitask reject'))
    installWizardHandlers()
    const user = userEvent.setup()
    const { router } = renderWizard()

    await fillScope(user, screen)
    await user.click(screen.getByRole('button', { name: 'Launch Pipeline' }))

    expect(await screen.findByText('Launch failed: multitask reject')).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/runs/new')
    expect(screen.getByRole('button', { name: 'Launch Pipeline' })).toBeEnabled() // retryable
  })
})
