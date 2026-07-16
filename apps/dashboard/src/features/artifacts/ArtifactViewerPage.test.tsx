import { screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'

import {
  artifactHandlers,
  PIPELINE_DETAIL,
  pipelineDetailHandler,
  renderRunRoutes,
  THREAD_ID,
} from '@/features/runs/__tests__/testUtils'
import { server } from '@/test/server'

import { MAX_ARTIFACT_INLINE_PREVIEW_BYTES } from './ArtifactViewerPage'
import { artifactViewerPath } from './artifactPaths'

vi.mock('@uiw/react-codemirror', async () => {
  const { createElement } = await import('react')
  return {
    default: ({ value }: { value: string }) =>
      createElement('pre', { 'data-testid': 'codemirror' }, value),
  }
})

describe('ArtifactViewerPage', () => {
  it('renders a JSON artifact through the JsonViewer with header metadata', async () => {
    server.use(pipelineDetailHandler(), ...artifactHandlers)
    renderRunRoutes([`/runs/${THREAD_ID}/artifacts/exec-report`])

    const viewer = await screen.findByTestId('codemirror')
    // Pretty-printed through JSON.parse -> stringify(…, 2).
    expect(viewer).toHaveTextContent('"tps_avg": 42.5')
    expect(screen.getByText('load-report.json')).toBeInTheDocument()
    expect(screen.getByText('report')).toBeInTheDocument() // kind chip
    expect(screen.getByText('application/json')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled()
  })

  it('falls back to a download card for binary media types', async () => {
    server.use(pipelineDetailHandler(), ...artifactHandlers)
    renderRunRoutes([`/runs/${THREAD_ID}/artifacts/exec-archive`])

    const card = await screen.findByTestId('binary-download-card')
    expect(card).toHaveTextContent('application/octet-stream')
    expect(card).toHaveTextContent('6 B')
    expect(
      screen.getByRole('button', { name: 'Download results.zip' }),
    ).toBeInTheDocument()
    expect(screen.queryByTestId('codemirror')).not.toBeInTheDocument()
  })

  it('keeps oversized text artifacts out of the inline editor', async () => {
    const largeArtifact = {
      id: 'large-log',
      kind: 'log',
      name: 'large.log',
      uri: 'apex-artifact:///logs/thread-1/large.log',
      media_type: 'text/plain',
      summary: 'Large execution log',
    }
    server.use(
      pipelineDetailHandler({
        ...PIPELINE_DETAIL,
        values: {
          ...PIPELINE_DETAIL.values,
          artifacts: [
            ...((PIPELINE_DETAIL.values?.artifacts as unknown[]) ?? []),
            largeArtifact,
          ],
        },
      }),
      http.get('*/v1/artifacts/logs/thread-1/large.log', () =>
        new HttpResponse('x'.repeat(MAX_ARTIFACT_INLINE_PREVIEW_BYTES + 1), {
          headers: { 'Content-Type': 'text/plain' },
        }),
      ),
    )
    renderRunRoutes([`/runs/${THREAD_ID}/artifacts/large-log`])

    const card = await screen.findByTestId('large-artifact-download-card')
    expect(card).toHaveTextContent('Preview unavailable')
    expect(card).toHaveTextContent('too large for a safe inline preview')
    expect(screen.queryByTestId('codemirror')).not.toBeInTheDocument()
  })

  it('opens artifact ids containing reserved path delimiters', async () => {
    const artifactId = 'report/with ?# delimiters'
    server.use(
      pipelineDetailHandler({
        ...PIPELINE_DETAIL,
        values: {
          ...PIPELINE_DETAIL.values,
          artifacts: [
            ...((PIPELINE_DETAIL.values?.artifacts as unknown[]) ?? []),
            {
              id: artifactId,
              kind: 'report',
              name: 'reserved-id.txt',
              uri: 'memory://reports/thread-1/reserved-id.txt',
              media_type: 'text/plain',
            },
          ],
        },
      }),
      http.get('*/v1/artifacts/reports/thread-1/reserved-id.txt', () =>
        new HttpResponse('reserved id opened', {
          headers: { 'Content-Type': 'text/plain' },
        }),
      ),
    )
    renderRunRoutes([artifactViewerPath(THREAD_ID, artifactId)])

    expect(await screen.findByTestId('codemirror')).toHaveTextContent('reserved id opened')
  })

  it('shows a not-found empty state for unknown artifact ids', async () => {
    server.use(pipelineDetailHandler())
    renderRunRoutes([`/runs/${THREAD_ID}/artifacts/no-such-artifact`])

    expect(await screen.findByText('Artifact not found')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to run' })).toHaveAttribute(
      'href',
      `/runs/${THREAD_ID}`,
    )
  })
})
