import { Link, useParams } from 'react-router'

import { useQuery } from '@tanstack/react-query'

import { PHASE_NAMES, type ArtifactRef, type PipelineState } from '@apex/pipeline-events'

import { useThreadState } from '@/api/hooks/useThreadState'
import { queryKeys } from '@/api/queryKeys'
import { ProblemCard } from '@/components/ProblemCard'
import { CodeViewer } from '@/components/viewers/CodeViewer'
import { JsonViewer } from '@/components/viewers/JsonViewer'

import { artifactProxyUrl, fetchArtifactBytes } from './artifactUrl'
import './artifact-viewer.css'

/**
 * Find an ArtifactRef by id: the run-level artifacts index first, then each
 * phase's transcript_ref (transcripts are also appended to the index by
 * finalize, but tolerate states where only the entry carries the ref).
 */
export function findArtifact(state: PipelineState, artifactId: string): ArtifactRef | undefined {
  const indexed = state.artifacts?.find((artifact) => artifact.id === artifactId)
  if (indexed) return indexed
  for (const phase of PHASE_NAMES) {
    const transcript = state.phase_results?.[phase]?.transcript_ref
    if (transcript && transcript.id === artifactId) return transcript
  }
  return undefined
}

type RenderKind = 'json' | 'text' | 'binary'

function renderKindOf(mediaType: string): RenderKind {
  const normalized = mediaType.toLowerCase()
  if (normalized.includes('json')) return 'json'
  if (normalized.startsWith('text/')) return 'text'
  return 'binary'
}

interface LoadedArtifact {
  kind: RenderKind
  blob: Blob
  mediaType: string
  size: number
  /** Decoded body for json/text kinds; undefined for binary. */
  text?: string
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

/**
 * /runs/:threadId/artifacts/:name — :name is the ARTIFACT ID. Resolves the ref
 * from thread state, streams bytes through the /v1/artifacts proxy, and renders
 * JSON/text inline (CodeMirror) or a download card for binaries.
 */
export function ArtifactViewerPage() {
  const { threadId = '', name = '' } = useParams()
  const threadQuery = useThreadState(threadId)

  const ref = threadQuery.data ? findArtifact(threadQuery.data.state, name) : undefined
  const url = ref ? artifactProxyUrl(ref.uri) : null

  const artifactQuery = useQuery({
    queryKey: queryKeys.threads.artifact(threadId, name),
    enabled: url !== null,
    staleTime: Infinity, // artifact bytes are immutable once written
    queryFn: async ({ signal }): Promise<LoadedArtifact> => {
      const bytes = await fetchArtifactBytes(url as string, signal)
      const mediaType = bytes.mediaType || ref?.media_type || ''
      const kind = renderKindOf(mediaType)
      return {
        kind,
        blob: bytes.blob,
        mediaType,
        size: bytes.size,
        text: kind === 'binary' ? undefined : await bytes.blob.text(),
      }
    },
  })

  if (threadQuery.isPending) {
    return (
      <div
        className="glass-panel artifact-viewer-skeleton"
        data-testid="artifact-skeleton"
        aria-busy="true"
      />
    )
  }
  if (threadQuery.isError) {
    return (
      <ProblemCard
        title="Artifact failed to load"
        message={threadQuery.error instanceof Error ? threadQuery.error.message : 'Unknown error'}
        onRetry={() => void threadQuery.refetch()}
      />
    )
  }

  if (!ref) {
    return (
      <div className="dash-empty">
        <h2>Artifact not found</h2>
        <p>No artifact with id “{name}” exists in this run’s state.</p>
        <Link className="btn btn-secondary btn-sm" to={`/runs/${threadId}`}>
          Back to run
        </Link>
      </div>
    )
  }

  const loaded = artifactQuery.data

  return (
    <>
      <header className="artifact-viewer-header glass-panel">
        <span className="artifact-viewer-name">{ref.name ?? ref.id}</span>
        <span className="kind-chip">{ref.kind ?? 'artifact'}</span>
        <span className="meta">{loaded?.mediaType || ref.media_type || 'unknown type'}</span>
        {loaded && <span className="meta">{formatBytes(loaded.size)}</span>}
        <span className="spacer" />
        <Link className="btn btn-ghost btn-sm" to={`/runs/${threadId}`}>
          Back to run
        </Link>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          disabled={!loaded}
          onClick={() => loaded && downloadBlob(loaded.blob, ref.name ?? ref.id)}
        >
          Download
        </button>
      </header>

      {url === null ? (
        <div className="dash-empty artifact-download-card">
          <h2>Not proxyable</h2>
          <p>
            This artifact’s uri ({ref.uri ?? 'absent'}) is not served by the /v1/artifacts proxy.
          </p>
        </div>
      ) : artifactQuery.isPending ? (
        <div
          className="glass-panel artifact-viewer-skeleton"
          data-testid="artifact-skeleton"
          aria-busy="true"
        />
      ) : artifactQuery.isError ? (
        <ProblemCard
          title="Artifact bytes failed to load"
          message={
            artifactQuery.error instanceof Error ? artifactQuery.error.message : 'Unknown error'
          }
          onRetry={() => void artifactQuery.refetch()}
        />
      ) : loaded?.kind === 'json' ? (
        <div className="artifact-viewer-body">
          <JsonViewer value={loaded.text ?? ''} ariaLabel={`${ref.name ?? ref.id} JSON contents`} />
        </div>
      ) : loaded?.kind === 'text' ? (
        <div className="artifact-viewer-body">
          <CodeViewer value={loaded.text ?? ''} ariaLabel={`${ref.name ?? ref.id} contents`} />
        </div>
      ) : (
        <div className="dash-empty artifact-download-card" data-testid="binary-download-card">
          <h2>Binary artifact</h2>
          <div className="artifact-download-facts">
            <span>{loaded?.mediaType || 'application/octet-stream'}</span>
            <span>{loaded ? formatBytes(loaded.size) : ''}</span>
          </div>
          <p>No inline preview for this media type — download to inspect.</p>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => loaded && downloadBlob(loaded.blob, ref.name ?? ref.id)}
          >
            Download {ref.name ?? ref.id}
          </button>
        </div>
      )}
    </>
  )
}
