/**
 * /prompts/:ns/:name/versions/:v — version content + meta, and ?diff= mode
 * rendering added/removed lines through @codemirror/merge's unifiedMergeView
 * (REAL CodeMirror here — the probe showed it renders chunks in jsdom).
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeAll, describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import { promptCatalog } from './promptsTestHandlers'

const V1_URL = '/prompts/phase/story_analysis%2Fsystem/versions/v-1'
const V2_URL = '/prompts/phase/story_analysis%2Fsystem/versions/v-2'

// jsdom Ranges have no layout; CodeMirror's measure cycle catches the missing
// getClientRects and logs it — polyfill empty rects to keep stderr quiet.
beforeAll(() => {
  const emptyRects = () => {
    const list = [] as unknown as DOMRectList
    ;(list as unknown as { item: (i: number) => DOMRect | null }).item = () => null
    return list
  }
  Range.prototype.getClientRects = emptyRects
  Range.prototype.getBoundingClientRect = () =>
    ({ x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }) as DOMRect
})

describe('PromptVersionPage', () => {
  it('renders the version content, meta and rollback affordance', async () => {
    server.use(...promptCatalog().handlers)
    renderApp({ initialEntries: [V1_URL], authState: authenticatedState() })

    const breadcrumb = await screen.findByRole('navigation', { name: 'Breadcrumb' })
    expect(within(breadcrumb).getByText('v1')).toBeInTheDocument()
    expect(screen.getByText(/initial draft/)).toBeInTheDocument()
    expect(screen.getByText(/alice/)).toBeInTheDocument()
    // v1 content rendered by the read-only viewer
    expect(await screen.findByText('Be terse.')).toBeInTheDocument()
    // v1 is not active -> rollback offered to operators+
    expect(
      screen.getByRole('button', { name: 'Set this version active' }),
    ).toBeInTheDocument()
  })

  it('?diff= renders added and removed lines against the selected version', async () => {
    server.use(...promptCatalog().handlers)
    const { container, router } = renderApp({
      initialEntries: [V2_URL],
      authState: authenticatedState(),
    })

    // pick the comparison version from the history dropdown -> writes ?diff=
    const picker = await screen.findByRole('combobox', { name: 'Compare with version' })
    await userEvent.selectOptions(picker, 'v-1')
    expect(router.state.location.search).toContain('diff=v-1')

    await waitFor(() => expect(container.querySelector('.cm-deletedChunk')).not.toBeNull())
    // line only in v1 renders as deleted; lines only in v2 render as inserted
    expect(container.querySelector('.cm-deletedChunk')).toHaveTextContent('Be terse.')
    const inserted = [...container.querySelectorAll('.cm-insertedLine')]
      .map((node) => node.textContent)
      .join('\n')
    expect(inserted).toContain('Be thorough.')
    expect(inserted).toContain('Cite evidence for every claim.')
  })
})
