import { formatBytes } from '@/utils/formatBytes'
import { Skeleton } from '@/components/ux/Skeleton'
import { StateCard } from '@/components/ux/StateCard'
import { useState } from 'react'
import Markdown from 'react-markdown'
import { artifactReturnPath } from './ArtifactLink'
import { Link, useLocation, useParams } from 'react-router'

import { useQuery } from '@tanstack/react-query'

import { parseGateInterrupt, PHASE_NAMES, type ArtifactRef, type PipelineState } from '@apex/pipeline-events'

import { useThreadState } from '@/api/hooks/useThreadState'
import { queryKeys } from '@/api/queryKeys'
import { CachedDataWarning } from '@/components/CachedDataWarning'
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

type RenderKind = 'json' | 'text' | 'binary' | 'download'

export const MAX_ARTIFACT_INLINE_PREVIEW_BYTES = 1024 * 1024

function renderKindOf(mediaType: string): RenderKind {
  const normalized = mediaType.toLowerCase()
  if (normalized.includes('json')) return 'json'
  if (normalized.startsWith('text/')) return 'text'
  return 'binary'
}

function parseTranscript(text: string): (Record<string, unknown> & { resolved_prompt?: Record<string, unknown> }) | null {
  try {
    const value: unknown = JSON.parse(text.slice(text.indexOf('\n') + 1))
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null
    const record = value as Record<string, unknown>
    const prompt = record.resolved_prompt
    return { ...record, resolved_prompt: prompt && typeof prompt === 'object' && !Array.isArray(prompt) ? prompt as Record<string, unknown> : {} }
  } catch { return null }
}

interface LoadedArtifact {
  kind: RenderKind
  blob: Blob
  mediaType: string
  size: number
  /** Decoded body for json/text kinds; undefined for binary. */
  text?: string
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.style.display = 'none'
  document.body.append(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

/**
 * /runs/:threadId/artifacts/:name — :name is the ARTIFACT ID. Resolves the ref
 * from thread state, streams bytes through the /v1/artifacts proxy, and renders
 * JSON/text inline (CodeMirror) or a download card for binaries.
 */
export function ArtifactViewerPage() {
  const { threadId = '', name = '' } = useParams()
  const location = useLocation()
  const returnPath = artifactReturnPath(location.state?.artifactReturnTo, threadId)
  const returnLabel = returnPath.includes('/reviews/')
    ? 'Back to review'
    : returnPath.startsWith('/approvals')
      ? 'Back to review'
      : returnPath.includes('/phases/')
        ? 'Back to phase'
        : 'Back to run'
  const [sourceFor, setSourceFor] = useState<string | null>(null)
  const showingSource = sourceFor === name
  const threadQuery = useThreadState(threadId)

  const pendingTranscript = threadQuery.data?.interrupts.map(interrupt => parseGateInterrupt(interrupt.payload)).find(payload => payload?.kind === 'phase_review' && payload.evidence?.transcript_ref?.id === name)
  const ref = (threadQuery.data ? findArtifact(threadQuery.data.state, name) : undefined) ?? (pendingTranscript?.kind === 'phase_review' ? pendingTranscript.evidence?.transcript_ref ?? undefined : undefined)
  const url = ref ? artifactProxyUrl(ref.uri) : null

  const artifactQuery = useQuery({
    queryKey: queryKeys.threads.artifact(threadId, name),
    enabled: url !== null,
    staleTime: Infinity, // artifact bytes are immutable once written
    // Artifact blobs can be tens of MiB. Release an inactive viewer entry
    // immediately instead of retaining several prior downloads in the default
    // five-minute query cache.
    gcTime: 0,
    queryFn: async ({ signal }): Promise<LoadedArtifact> => {
      const bytes = await fetchArtifactBytes(url as string, signal)
      const mediaType = bytes.mediaType || ref?.media_type || ''
      const detectedKind = renderKindOf(mediaType)
      const kind =
        detectedKind !== 'binary' && bytes.size > MAX_ARTIFACT_INLINE_PREVIEW_BYTES
          ? 'download'
          : detectedKind
      return {
        kind,
        blob: bytes.blob,
        mediaType,
        size: bytes.size,
        text: kind === 'json' || kind === 'text' ? await bytes.blob.text() : undefined,
      }
    },
  })

  if (threadQuery.isPending) {
    return (
      <Skeleton label="Loading artifact" rows={2} height={120} data-testid="artifact-skeleton" />
    )
  }
  if (threadQuery.isError && !threadQuery.data) {
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
      <StateCard kind="not-found" title="Artifact not found" action={<Link className="btn btn-secondary btn-sm" to={returnPath}>{returnLabel}</Link>}>
        No artifact with id “{name}” exists in this run’s state.
      </StateCard>
    )
  }

  const loaded = artifactQuery.data
  const transcript = loaded?.kind === 'text' && loaded.text?.startsWith('APEX phase transcript\n')
    ? parseTranscript(loaded.text) : null

  return (
    <>
      {threadQuery.isError && (
        <CachedDataWarning
          error={threadQuery.error}
          onRetry={() => void threadQuery.refetch()}
        />
      )}
      <header className="artifact-viewer-header glass-panel">
        <span className="artifact-viewer-name">{ref.name ?? ref.id}</span>
        <span className="kind-chip">{ref.kind ?? 'artifact'}</span>
        <span className="meta">{loaded?.mediaType || ref.media_type || 'unknown type'}</span>
        {loaded && <span className="meta">{formatBytes(loaded.size)}</span>}
        <span className="spacer" />
        <Link className="btn btn-ghost btn-sm" to={returnPath}>
          {returnLabel}
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

      {artifactQuery.isError && loaded && (
        <CachedDataWarning
          error={artifactQuery.error}
          onRetry={() => void artifactQuery.refetch()}
        />
      )}

      {url === null ? (
        <div className="glass-panel ux-card artifact-download-card">
          <h2>Not proxyable</h2>
          <p>
            This artifact’s uri ({ref.uri ?? 'absent'}) is not served by the /v1/artifacts proxy.
          </p>
        </div>
      ) : artifactQuery.isPending ? (
        <Skeleton label="Loading artifact" rows={2} height={120} data-testid="artifact-skeleton" />
      ) : artifactQuery.isError && !loaded ? (
        <ProblemCard
          title="Artifact bytes failed to load"
          message={
            artifactQuery.error instanceof Error ? artifactQuery.error.message : 'Unknown error'
          }
          onRetry={() => void artifactQuery.refetch()}
        />
      ) : transcript ? (
        <section className="artifact-viewer-body" aria-label="Full agent transcript">
          <h2>Full agent transcript</h2>
          {Object.entries(transcript.resolved_prompt ?? {}).map(([field, value]) => typeof value === 'string' ? <section key={field}><h3>{field.replaceAll('_', ' ')} prompt</h3><CodeViewer value={value} ariaLabel={`${field} prompt`} /></section> : null)}
          <JsonViewer value={transcript} ariaLabel="Transcript record" />
        </section>
      ) : loaded?.kind === 'json' ? (
        <div className="artifact-viewer-body">
          <JsonViewer value={loaded.text ?? ''} ariaLabel={`${ref.name ?? ref.id} JSON contents`} />
        </div>
      ) : loaded?.kind === 'text' ? (
        <div className="artifact-viewer-body">
          {loaded.mediaType.split(';')[0]?.trim().toLowerCase() === 'text/markdown' ? <>
            <button className="btn btn-secondary" aria-pressed={showingSource} onClick={() => setSourceFor(showingSource ? null : name)}>{showingSource ? 'Rendered report' : 'Source'}</button>
            {showingSource ? <CodeViewer value={loaded.text ?? ''} ariaLabel={`${ref.name ?? ref.id} source`} /> :
              <article className="markdown-report" aria-label={`${ref.name ?? ref.id} contents`}>
                <Markdown skipHtml urlTransform={(url) => /^(https?:|mailto:)/i.test(url) || url.startsWith('#') ? url : ''} components={{ img: ({ alt }) => <span>{alt ?? 'Image'}</span> }}>{loaded.text ?? ''}</Markdown>
              </article>}
          </> : <CodeViewer value={loaded.text ?? ''} ariaLabel={`${ref.name ?? ref.id} contents`} />}
        </div>
      ) : (
        <div
          className="glass-panel ux-card artifact-download-card"
          data-testid={loaded?.kind === 'download' ? 'large-artifact-download-card' : 'binary-download-card'}
        >
          <h2>{loaded?.kind === 'download' ? 'Preview unavailable' : 'Binary artifact'}</h2>
          <div className="artifact-download-facts">
            <span>{loaded?.mediaType || 'application/octet-stream'}</span>
            <span>{loaded ? formatBytes(loaded.size) : ''}</span>
          </div>
          <p>
            {loaded?.kind === 'download'
              ? 'This artifact is too large for a safe inline preview — download to inspect.'
              : 'No inline preview for this media type — download to inspect.'}
          </p>
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
