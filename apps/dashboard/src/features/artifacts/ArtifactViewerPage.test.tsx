import { screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import {
  artifactHandlers,
  pipelineDetailHandler,
  renderRunRoutes,
  THREAD_ID,
} from '@/features/runs/__tests__/testUtils'
import { server } from '@/test/server'

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
