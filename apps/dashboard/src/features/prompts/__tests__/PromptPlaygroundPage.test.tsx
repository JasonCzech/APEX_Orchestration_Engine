/**
 * /prompts/:ns/:name/playground — 202 accepted card + /runs/{thread_id} link
 * + session-local history; sample-input validation blocks bad JSON.
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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
      expect(catalog.calls.test).toEqual([{ version_id: 'v-2', sample_input: {} }]),
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
})
