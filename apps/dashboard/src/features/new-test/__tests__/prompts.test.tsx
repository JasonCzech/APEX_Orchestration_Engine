/**
 * Prompts step: only the focused phase system prompt and the selected
 * application's app-wide prompt are shown. System overrides keep
 * prompt_overrides["phase/<p>"]; application overrides use
 * prompt_overrides["application/<app_id>"].
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'

import { server } from '@/test/server'

import { installWizardHandlers, renderWizard } from './wizardTestUtils'

// CodeMirror needs real DOM measurement; mock it as a controlled textarea
// (same boundary mock the hitl/artifacts suites use).
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

const PHASE_PROMPTS = [
  {
    id: 'p-story-sys',
    namespace: 'phase',
    key: 'story_analysis/system',
    description: null,
    active_version: { id: 'v-2', version: 2 },
  },
  {
    id: 'p-exec-sys',
    namespace: 'phase',
    key: 'execution/system',
    description: null,
    active_version: { id: 'v-3', version: 3 },
  },
]

const APPLICATION_PROMPTS = [
  {
    id: 'p-app-checkout',
    namespace: 'application',
    key: 'app-checkout',
    description: null,
    active_version: { id: 'v-5', version: 5 },
  },
]

const PROMPT_CONTENT: Record<string, string> = {
  'p-story-sys': 'You are the story analysis phase operator.',
  'p-exec-sys': 'You are the execution phase operator.',
  'p-app-checkout': 'Checkout must preserve carts during payment retries.',
}

function promptHandlers({ includeApplication = true }: { includeApplication?: boolean } = {}) {
  const applicationPrompts = includeApplication ? APPLICATION_PROMPTS : []
  const allPrompts = [...PHASE_PROMPTS, ...applicationPrompts]
  return [
    http.get('*/v1/prompts', ({ request }) => {
      const namespace = new URL(request.url).searchParams.get('namespace')
      return HttpResponse.json(namespace === 'application' ? applicationPrompts : PHASE_PROMPTS)
    }),
    http.get('*/v1/prompts/:id', ({ params }) => {
      const id = params['id'] as string
      const summary = allPrompts.find((entry) => entry.id === id)
      if (!summary) return HttpResponse.json({ detail: 'not found' }, { status: 404 })
      return HttpResponse.json({ ...summary, content: PROMPT_CONTENT[id] })
    }),
  ]
}

async function selectCheckoutApplication(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('tab', { name: 'Scope' }))
  await screen.findByRole('option', { name: 'Checkout' })
  await user.selectOptions(screen.getByLabelText('Application'), 'app-checkout')
}

describe('PromptsStep', () => {
  it('shows only the focused phase system prompt plus an app-selection placeholder', async () => {
    installWizardHandlers()
    server.use(...promptHandlers())
    renderWizard('/runs/new?step=prompts')

    const promptSection = screen.getByRole('tabpanel', { name: 'Prompts' })
    await waitFor(() =>
      expect(within(promptSection).getByTestId('prompt-focused-phase')).toHaveTextContent(
        'story analysis',
      ),
    )

    const systemBlock = within(promptSection).getByTestId('system-prompt-block')
    await waitFor(() => expect(within(systemBlock).getByText('catalog@v2')).toBeInTheDocument())
    expect(within(systemBlock).getByTestId('codemirror')).toHaveValue(
      'You are the story analysis phase operator.',
    )

    expect(within(promptSection).queryByText('You are the execution phase operator.')).toBeNull()
    expect(within(promptSection).queryByText('User prompt')).toBeNull()
    expect(
      within(promptSection).getByText('Select an application in Scope to load its requirements prompt.'),
    ).toBeInTheDocument()
  })

  it('changes the displayed system prompt when Config changes focus', async () => {
    installWizardHandlers()
    server.use(...promptHandlers())
    const user = userEvent.setup()
    renderWizard('/runs/new?step=prompts')

    const promptSection = screen.getByRole('tabpanel', { name: 'Prompts' })
    await waitFor(() =>
      expect(within(promptSection).getByTestId('system-prompt-block')).toHaveTextContent(
        'System prompt',
      ),
    )
    expect(within(promptSection).getByTestId('prompt-focused-phase')).toHaveTextContent(
      'story analysis',
    )

    await user.click(screen.getByRole('tab', { name: 'Config' }))
    const strip = await screen.findByRole('group', { name: 'Phase subset' })
    await user.click(within(strip).getByRole('button', { name: 'execution' }))

    await user.click(screen.getByRole('tab', { name: 'Prompts' }))
    const focusedSection = screen.getByRole('tabpanel', { name: 'Prompts' })
    await waitFor(() =>
      expect(within(focusedSection).getByTestId('prompt-focused-phase')).toHaveTextContent(
        'execution',
      ),
    )
    const systemBlock = within(focusedSection).getByTestId('system-prompt-block')
    await waitFor(() =>
      expect(within(systemBlock).getByTestId('codemirror')).toHaveValue(
        'You are the execution phase operator.',
      ),
    )
    expect(within(focusedSection).queryByText('You are the story analysis phase operator.')).toBeNull()
    expect(
      within(focusedSection).getByText('Select an application in Scope to load its requirements prompt.'),
    ).toBeInTheDocument()
  })

  it('shows the selected application prompt from the application catalog', async () => {
    installWizardHandlers()
    server.use(...promptHandlers())
    const user = userEvent.setup()
    renderWizard()

    await selectCheckoutApplication(user)
    await user.click(screen.getByRole('tab', { name: 'Prompts' }))

    const promptSection = screen.getByRole('tabpanel', { name: 'Prompts' })
    await waitFor(() =>
      expect(within(promptSection).getByTestId('prompt-selected-application')).toHaveTextContent(
        'app-checkout',
      ),
    )
    const appBlock = within(promptSection).getByTestId('application-prompt-block')
    await waitFor(() => expect(within(appBlock).getByText('catalog@v5')).toBeInTheDocument())
    expect(within(appBlock).getByTestId('codemirror')).toHaveValue(
      'Checkout must preserve carts during payment retries.',
    )
  })

  it('keeps a missing application prompt as an empty overrideable slot', async () => {
    installWizardHandlers()
    server.use(...promptHandlers({ includeApplication: false }))
    const user = userEvent.setup()
    renderWizard()

    await selectCheckoutApplication(user)
    await user.click(screen.getByRole('tab', { name: 'Prompts' }))

    const promptSection = screen.getByRole('tabpanel', { name: 'Prompts' })
    const appBlock = await within(promptSection).findByTestId('application-prompt-block')
    expect(within(appBlock).getByText('No application prompt exists for this app yet.')).toBeInTheDocument()
    expect(within(appBlock).getByText('empty')).toBeInTheDocument()

    await user.click(within(appBlock).getByRole('button', { name: 'Override for this run' }))
    const editor = within(appBlock).getByLabelText('app-checkout application prompt override')
    expect(editor).toHaveValue('')
    await user.type(editor, 'Custom application requirements')

    await user.click(screen.getByRole('tab', { name: 'Review' }))
    const json = await screen.findByTestId('launch-payload-json')
    const payload = JSON.parse(json.textContent ?? '{}') as {
      configurable: { prompt_overrides?: Record<string, { content: string }> }
    }
    expect(payload.configurable.prompt_overrides).toEqual({
      'application/app-checkout': { content: 'Custom application requirements' },
    })
  })

  it('includes both system and application overrides in the review launch JSON', async () => {
    installWizardHandlers()
    server.use(...promptHandlers())
    const user = userEvent.setup()
    renderWizard()

    await selectCheckoutApplication(user)
    await user.click(screen.getByRole('tab', { name: 'Prompts' }))

    const promptSection = screen.getByRole('tabpanel', { name: 'Prompts' })
    const systemBlock = await within(promptSection).findByTestId('system-prompt-block')
    await waitFor(() => expect(within(systemBlock).getByText('catalog@v2')).toBeInTheDocument())
    await user.click(within(systemBlock).getByRole('button', { name: 'Override for this run' }))
    const systemEditor = within(systemBlock).getByLabelText('story_analysis system prompt override')
    await user.clear(systemEditor)
    await user.type(systemEditor, 'Custom story system prompt')

    const appBlock = await within(promptSection).findByTestId('application-prompt-block')
    await waitFor(() => expect(within(appBlock).getByText('catalog@v5')).toBeInTheDocument())
    await user.click(within(appBlock).getByRole('button', { name: 'Override for this run' }))
    const appEditor = within(appBlock).getByLabelText('app-checkout application prompt override')
    await user.clear(appEditor)
    await user.type(appEditor, 'Custom checkout requirements')

    await user.click(screen.getByRole('tab', { name: 'Review' }))
    const json = await screen.findByTestId('launch-payload-json')
    const payload = JSON.parse(json.textContent ?? '{}') as {
      configurable: { prompt_overrides?: Record<string, { content: string }> }
    }
    expect(payload.configurable.prompt_overrides).toEqual({
      'phase/story_analysis': { content: 'Custom story system prompt' },
      'application/app-checkout': { content: 'Custom checkout requirements' },
    })
  })
})
