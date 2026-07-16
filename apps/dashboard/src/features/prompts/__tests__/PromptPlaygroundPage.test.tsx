/**
 * /prompts/:ns/:name/playground — 202 accepted card + /runs/{thread_id} link
 * + session-local history; sample-input validation blocks bad JSON.
 */
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import { promptCatalog } from './promptsTestHandlers'

// Same CodeMirror-as-textarea boundary mock as the other editor suites.
vi.mock('@uiw/react-codemirror', async () => {
  const { createElement } = await import('react')
  return {
    default: ({
      value,
      onChange,
      'aria-label': ariaLabel,
    }: {
      value: string
      onChange?: (value: string) => void
      'aria-label'?: string
    }) =>
      createElement('textarea', {
        'data-testid': 'codemirror',
        'aria-label': ariaLabel,
        value,
        onChange: (event: { target: { value: string } }) => onChange?.(event.target.value),
      }),
  }
})

const PLAYGROUND_URL = '/prompts/phase/story_analysis%2Fsystem/playground'
const EXEC_PLAYGROUND_URL = '/prompts/phase/execution%2Fsystem/playground'

describe('PromptPlaygroundPage', () => {
  it('runs a version test: 202 -> accepted card, run link and history entry', async () => {
    const catalog = promptCatalog({ accept: { run_id: 'run-77', thread_id: 'thread-9' } })
    server.use(...catalog.handlers)
    renderApp({ initialEntries: [PLAYGROUND_URL], authState: authenticatedState('operator') })

    // version selector defaults to the active version
    const picker = await screen.findByRole('combobox', { name: 'Version to test' })
    await waitFor(() => expect(picker).toHaveValue('v-2'))
    expect(within(picker).getByText('v2 (active)')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Run test' }))

    await waitFor(() =>
      expect(catalog.calls.test).toEqual([
        { version_id: 'v-2', sample_input: {}, project_id: 'proj-alpha' },
      ]),
    )
    const accepted = within(await screen.findByTestId('playground-accepted'))
    expect(accepted.getByText('run-77')).toBeInTheDocument()
    expect(accepted.getByRole('link', { name: 'Open run' })).toHaveAttribute(
      'href',
      '/runs/thread-9',
    )
    const history = within(screen.getByTestId('playground-history'))
    expect(history.getByText('run-77')).toBeInTheDocument()
    expect(history.getByText(/v2 ·/)).toBeInTheDocument()
  })

  it('rejects invalid sample-input JSON without posting', async () => {
    const catalog = promptCatalog()
    server.use(...catalog.handlers)
    renderApp({ initialEntries: [PLAYGROUND_URL], authState: authenticatedState('operator') })

    const input = await screen.findByRole('textbox', { name: 'Sample input JSON' })
    await userEvent.clear(input)
    await userEvent.type(input, 'not json')
    await userEvent.click(screen.getByRole('button', { name: 'Run test' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('not valid JSON')
    expect(catalog.calls.test).toHaveLength(0)
  })

  it('requires an application choice for consumers with multiple app-only scopes', async () => {
    const catalog = promptCatalog()
    server.use(...catalog.handlers)
    const base = authenticatedState('operator')
    if (base.status !== 'authenticated') throw new Error('expected authenticated test state')
    const consumer = {
      ...base.consumer,
      scopes: [
        { project_id: 'proj-alpha', app_id: 'app-one' },
        { project_id: 'proj-alpha', app_id: 'app-two' },
      ],
    }
    renderApp({
      initialEntries: [PLAYGROUND_URL],
      authState: { ...base, consumer, systemInfo: { ...base.systemInfo, consumer } },
    })

    await screen.findByRole('combobox', { name: 'Version to test' })
    const run = screen.getByRole('button', { name: 'Run test' })
    expect(run).toBeDisabled()
    await userEvent.selectOptions(
      screen.getByRole('combobox', { name: 'Playground application' }),
      'app-two',
    )
    await userEvent.click(run)

    await waitFor(() =>
      expect(catalog.calls.test).toEqual([
        {
          version_id: 'v-2',
          sample_input: {},
          project_id: 'proj-alpha',
          app_id: 'app-two',
        },
      ]),
    )
  })

  it('cannot submit a prior prompt draft during a cached route transition', async () => {
    const catalog = promptCatalog()
    const submissions: Array<{ promptId: string; body: Record<string, unknown> }> = []
    server.use(...catalog.handlers)
    server.use(
      http.post('*/v1/prompts/:promptId/test', async ({ params, request }) => {
        submissions.push({
          promptId: String(params.promptId),
          body: (await request.json()) as Record<string, unknown>,
        })
        return HttpResponse.json(
          { run_id: `run-${String(params.promptId)}`, thread_id: null },
          { status: 202 },
        )
      }),
    )
    const user = userEvent.setup()
    const { router } = renderApp({
      initialEntries: [EXEC_PLAYGROUND_URL],
      authState: authenticatedState('operator'),
    })

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: 'Version to test' })).toHaveValue('v-exec-1'),
    )
    await act(async () => router.navigate(PLAYGROUND_URL))
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: 'Version to test' })).toHaveValue('v-2'),
    )
    await user.click(screen.getByRole('button', { name: 'Ad-hoc content' }))
    const editor = screen.getByRole('textbox', { name: 'Ad-hoc prompt content' })
    await user.clear(editor)
    await user.type(editor, 'story-only draft that must never reach execution')

    // execution is already cached. Fire the action in the same turn as the
    // synchronous route commit, before passive effects could repair stale
    // route-local state.
    act(() => {
      void router.navigate(EXEC_PLAYGROUND_URL, { flushSync: true })
      fireEvent.click(screen.getByRole('button', { name: 'Run test' }))
    })

    await waitFor(() => expect(submissions).toHaveLength(1))
    expect(submissions[0]).toEqual({
      promptId: 'p-exec',
      body: {
        version_id: 'v-exec-1',
        sample_input: {},
        project_id: 'proj-alpha',
      },
    })
  })

  it('keeps a deferred test locked and publishes its accepted run after a route remount', async () => {
    const catalog = promptCatalog()
    let releaseRequest!: () => void
    const requestRelease = new Promise<void>((resolve) => {
      releaseRequest = resolve
    })
    const submissions: Record<string, unknown>[] = []
    server.use(...catalog.handlers)
    server.use(
      http.post('*/v1/prompts/p-story/test', async ({ request }) => {
        submissions.push((await request.json()) as Record<string, unknown>)
        await requestRelease
        return HttpResponse.json(
          { run_id: 'run-remount', thread_id: 'thread-remount' },
          { status: 202 },
        )
      }),
    )
    const user = userEvent.setup()
    const base = authenticatedState('operator')
    if (base.status !== 'authenticated') throw new Error('expected authenticated test state')
    const consumer = {
      ...base.consumer,
      scopes: [
        { project_id: 'proj-alpha', app_id: 'app-one' },
        { project_id: 'proj-alpha', app_id: 'app-two' },
      ],
    }
    const { router } = renderApp({
      initialEntries: [PLAYGROUND_URL],
      authState: { ...base, consumer, systemInfo: { ...base.systemInfo, consumer } },
    })

    await screen.findByRole('combobox', { name: 'Version to test' })
    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Playground application' }),
      'app-two',
    )
    await user.click(screen.getByRole('button', { name: 'Run test' }))
    await waitFor(() => expect(submissions).toHaveLength(1))

    await act(async () => router.navigate('/settings'))
    await act(async () => router.navigate(PLAYGROUND_URL))
    await screen.findByRole('combobox', { name: 'Version to test' })
    expect(screen.getByRole('combobox', { name: 'Playground application' })).toHaveValue(
      'app-two',
    )

    const pendingRun = screen.getByRole('button', { name: 'Submitting…' })
    expect(pendingRun).toBeDisabled()
    await user.click(pendingRun)
    expect(submissions).toHaveLength(1)

    releaseRequest()

    const accepted = within(await screen.findByTestId('playground-accepted'))
    expect(accepted.getByText('run-remount')).toBeInTheDocument()
    expect(accepted.getByRole('link', { name: 'Open run' })).toHaveAttribute(
      'href',
      '/runs/thread-remount',
    )
    const history = within(screen.getByTestId('playground-history'))
    expect(history.getByText('run-remount')).toBeInTheDocument()
    expect(submissions).toEqual([
      {
        version_id: 'v-2',
        sample_input: {},
        project_id: 'proj-alpha',
        app_id: 'app-two',
      },
    ])
  })

  it('ignores a test completion from the prompt route that was navigated away from', async () => {
    const catalog = promptCatalog()
    const started: string[] = []
    const completed: string[] = []
    server.use(...catalog.handlers)
    server.use(
      http.post('*/v1/prompts/:promptId/test', async ({ params }) => {
        const promptId = String(params.promptId)
        started.push(promptId)
        if (promptId === 'p-story') await delay(120)
        completed.push(promptId)
        return HttpResponse.json(
          { run_id: `run-${promptId}`, thread_id: `thread-${promptId}` },
          { status: 202 },
        )
      }),
    )
    const { router } = renderApp({
      initialEntries: [PLAYGROUND_URL],
      authState: authenticatedState('operator'),
    })

    await screen.findByRole('combobox', { name: 'Version to test' })
    await userEvent.click(screen.getByRole('button', { name: 'Run test' }))
    await waitFor(() => expect(started).toContain('p-story'))

    await act(async () => router.navigate(EXEC_PLAYGROUND_URL))
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: 'Version to test' })).toHaveValue('v-exec-1'),
    )
    await waitFor(() => expect(completed).toContain('p-story'))
    expect(screen.queryByTestId('playground-accepted')).not.toBeInTheDocument()
    expect(screen.queryByText('run-p-story')).not.toBeInTheDocument()
  })
})
