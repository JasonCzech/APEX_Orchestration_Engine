/**
 * Draft autosave: debounced 1.5s after the last change; the FIRST save
 * creates the draft (title fallback honored), later saves update the SAME id
 * (create-then-update sequencing), and the created id lands in the URL.
 *
 * Uses fireEvent + vi fake timers (setTimeout/clearTimeout only): userEvent's
 * async wrapper deadlocks under faked clocks, and typing fidelity is not what
 * these tests cover — the debounce timer is.
 */
import { act, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { installWizardHandlers, renderWizard } from './wizardTestUtils'

function setField(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } })
}

async function advanceDebounce() {
  await act(async () => {
    vi.advanceTimersByTime(1_500)
  })
}

describe('wizard draft autosave', () => {
  beforeEach(() => {
    // Fake ONLY the timer pair the debounce uses; fetch/msw stay on real IO.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('creates on first change after the debounce, then updates the same draft', async () => {
    const { captured } = installWizardHandlers()
    const { router } = renderWizard()

    setField('Title', 'Soak test')
    expect(captured.creates).toHaveLength(0) // debounce still pending
    expect(screen.getByTestId('draft-save-state')).toHaveTextContent('Unsaved changes')

    // A second change inside the window resets the timer — still no save…
    await act(async () => {
      vi.advanceTimersByTime(1_000)
    })
    setField('Request', 'Hammer checkout')
    await act(async () => {
      vi.advanceTimersByTime(1_000)
    })
    expect(captured.creates).toHaveLength(0)

    // …until 1.5s after the LAST change: exactly one create with the payload verbatim.
    await act(async () => {
      vi.advanceTimersByTime(500)
    })
    await vi.waitFor(() => expect(captured.creates).toHaveLength(1))
    expect(captured.creates[0]).toMatchObject({
      title: 'Soak test',
      project_id: 'demo',
      payload: { title: 'Soak test', request: 'Hammer checkout' },
    })

    // The new id lands in the URL (replace-history) and the chip flips to saved.
    await vi.waitFor(() => expect(router.state.location.search).toContain('draft=draft-1'))
    await vi.waitFor(() =>
      expect(screen.getByTestId('draft-save-state')).toHaveTextContent('Draft saved'),
    )

    // A later change UPDATES the same draft — no second create.
    setField('Project', 'proj-b')
    setField('Title', 'Soak test v2')
    await advanceDebounce()
    await vi.waitFor(() => expect(captured.updates).toHaveLength(1))
    expect(captured.creates).toHaveLength(1)
    expect(captured.updates[0]).toMatchObject({
      id: 'draft-1',
      title: 'Soak test v2',
      project_id: 'proj-b',
      payload: {
        title: 'Soak test v2',
        request: 'Hammer checkout',
        scope: { project_id: 'proj-b' },
      },
    })
  })

  it('falls back to "Untitled run" when saving before a title exists', async () => {
    const { captured } = installWizardHandlers()
    renderWizard()

    setField('Request', 'No title yet')
    await advanceDebounce()
    await vi.waitFor(() => expect(captured.creates).toHaveLength(1))
    expect(captured.creates[0]?.title).toBe('Untitled run')
  })

  it('flushes dirty state on unmount without creating an untouched draft', async () => {
    const { captured } = installWizardHandlers()
    const untouched = renderWizard()
    untouched.unmount()
    await act(async () => Promise.resolve())
    expect(captured.creates).toHaveLength(0)

    const dirty = renderWizard()
    setField('Title', 'Leave this page')
    dirty.unmount()
    await vi.waitFor(() => expect(captured.creates).toHaveLength(1))
    expect(captured.creates[0]).toMatchObject({
      title: 'Leave this page',
      payload: { title: 'Leave this page' },
    })
  })
})
