/**
 * /prompts/:ns/:name detail: new-version save + active bump, rollback via the
 * confirm modal (POST + cache invalidate), optimistic archive with
 * revert-on-error, viewer gating.
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import { promptCatalog, STORY_V2_CONTENT } from './promptsTestHandlers'

// Same CodeMirror-as-textarea boundary mock as the other editor suites.
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

const DETAIL_URL = '/prompts/phase/story_analysis%2Fsystem'

describe('PromptDetailPage', () => {
  it('saves a new version pre-filled from active and bumps the active pointer', async () => {
    const catalog = promptCatalog()
    server.use(...catalog.handlers)
    renderApp({ initialEntries: [DETAIL_URL], authState: authenticatedState() })

    expect(await screen.findByText('active v2')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'New version' }))

    const editor = screen.getByLabelText('New version content')
    expect(editor).toHaveValue(STORY_V2_CONTENT)
    expect(screen.getByText('No changes vs active')).toBeInTheDocument()
    // unchanged content cannot be saved
    expect(screen.getByRole('button', { name: 'Save as v3' })).toBeDisabled()

    await userEvent.type(editor, '\nAlways cite line numbers.')
    expect(screen.getByLabelText('Changes vs active')).toHaveTextContent('+1 −0 lines vs active')

    await userEvent.type(screen.getByLabelText('Version note'), 'cite lines')
    await userEvent.click(screen.getByRole('button', { name: 'Save as v3' }))

    await waitFor(() => expect(catalog.calls.saveVersion).toHaveLength(1))
    expect(catalog.calls.saveVersion[0]).toEqual({
      content: `${STORY_V2_CONTENT}\nAlways cite line numbers.`,
      note: 'cite lines',
    })
    // invalidation refetches the detail — the header chip bumps to v3
    expect(await screen.findByText('active v3')).toBeInTheDocument()
    expect(screen.queryByLabelText('New version content')).not.toBeInTheDocument()
  })

  it('rolls back through the confirm modal and invalidates the cache', async () => {
    const catalog = promptCatalog()
    server.use(...catalog.handlers)
    renderApp({
      initialEntries: [`${DETAIL_URL}?tab=versions`],
      authState: authenticatedState(),
    })

    // newest-first timeline with the active marker on v2
    const v2 = within(await screen.findByTestId('prompt-version-v-2'))
    expect(v2.getByText('active')).toBeInTheDocument()
    const v1 = within(screen.getByTestId('prompt-version-v-1'))
    expect(v1.getByText('initial draft')).toBeInTheDocument()

    await userEvent.click(v1.getByRole('button', { name: 'Set active' }))
    const modal = within(screen.getByRole('dialog', { name: 'Set v1 active' }))
    expect(modal.getByText(/initial draft/)).toBeInTheDocument()
    await userEvent.click(modal.getByRole('button', { name: 'Set v1 active' }))

    await waitFor(() => expect(catalog.calls.rollback).toEqual([{ version_id: 'v-1' }]))
    // cache invalidated: header chip and timeline marker move to v1
    expect(await screen.findByText('active v1')).toBeInTheDocument()
    await waitFor(() =>
      expect(
        within(screen.getByTestId('prompt-version-v-1')).getByText('active'),
      ).toBeInTheDocument(),
    )
  })

  it('archives optimistically and reverts on a 500', async () => {
    const catalog = promptCatalog()
    server.use(...catalog.handlers)
    server.use(
      http.post('*/v1/prompts/p-story/archive', async () => {
        await delay(120)
        return HttpResponse.json({ detail: 'catalog write failed' }, { status: 500 })
      }),
    )
    renderApp({ initialEntries: [DETAIL_URL], authState: authenticatedState() })

    await screen.findByText('active v2')
    expect(screen.queryByText('archived')).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Archive' }))
    // optimistic: the chip flips before the server answers
    expect(await screen.findByText('archived')).toBeInTheDocument()

    // 500 lands: revert + error surface
    await waitFor(() => expect(screen.queryByText('archived')).not.toBeInTheDocument())
    expect(screen.getByRole('alert')).toHaveTextContent('catalog write failed')
    expect(screen.getByRole('button', { name: 'Archive' })).toBeInTheDocument()
  })

  it('hides every mutation from viewers', async () => {
    server.use(...promptCatalog().handlers)
    renderApp({
      initialEntries: [`${DETAIL_URL}?tab=versions`],
      authState: authenticatedState('viewer'),
    })

    await screen.findByTestId('prompt-version-v-1')
    expect(screen.queryByRole('button', { name: 'New version' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Archive' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Set active' })).not.toBeInTheDocument()
    // read affordances stay
    expect(screen.getByRole('link', { name: 'Test in playground' })).toBeInTheDocument()
    expect(screen.getAllByRole('link', { name: 'View' }).length).toBeGreaterThan(0)
  })
})
