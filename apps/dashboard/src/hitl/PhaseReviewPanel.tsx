/**
 * phase_review gate panel (plan 2.a): summary block, result_preview JSON-ish
 * view, artifact chips (linking into the run's artifact viewer), warning
 * cards, and the DialogueThread (dialogue_tail + discuss composer).
 */
import { ArtifactLink } from '@/features/artifacts/ArtifactLink'

import type { PhaseReviewPayload } from '@apex/pipeline-events'

import { ReasoningTab } from '@/features/runs/PhaseWorkspace'
import { CodeViewer } from '@/components/viewers/CodeViewer'
import { artifactViewerPath } from '@/features/artifacts/artifactPaths'

import { DialogueThread } from './DialogueThread'
import type { GateDraft, GateDraftPatch } from './gateMachine'

export function PhaseReviewPanel({
  threadId,
  returnTo,
  payload,
  draft,
  disabled,
  compact = false,
  onEdit,
}: {
  threadId: string
  returnTo?: string
  payload: PhaseReviewPayload
  draft: GateDraft
  disabled: boolean
  compact?: boolean
  onEdit: (patch: GateDraftPatch) => void
}) {
  const paragraphs = payload.summary?.split(/\n{2,}/).filter((p) => p.trim().length > 0) ?? []
  const preview = payload.result_preview
  const previewEntries = Object.entries(preview).filter(([, value]) => value != null)

  return (
    <div className="gate-panel phase-review-layout" data-testid="phase-review-panel">
      <div className="phase-review-main">
        {payload.projection_truncated === true && <p role="note" className="tonal-card warning">
          Some output or discussion was omitted from this review preview because it exceeded the display limit.
        </p>}
        {paragraphs.length > 0 ? (
          <div className="gate-summary" data-testid="gate-summary">
            {paragraphs.map((paragraph, index) => (
              <p key={index}>{paragraph}</p>
            ))}
          </div>
        ) : (
          <div className="dash-empty small">{payload.projection_truncated === true ? 'The phase summary is unavailable in this review preview.' : 'No summary provided for this phase result.'}</div>
        )}

        {payload.warnings.map((warning, index) => (
          <div key={index} className="tonal-card warning" data-testid="gate-warning">
            {warning}
          </div>
        ))}

        {payload.artifacts.length > 0 && (
          <div className="gate-info-card gate-artifacts" data-testid="gate-artifacts">
            <h4 className="gate-field-label">Artifacts</h4>
            <div className="gate-chip-row">
              {payload.artifacts.map((artifact, index) =>
                artifact.id ? (
                  <ArtifactLink
                    returnTo={returnTo}
                    key={artifact.id}
                    className="gate-artifact-chip"
                    to={artifactViewerPath(threadId, artifact.id)}
                  >
                    <span className="kind-chip">{artifact.kind ?? 'artifact'}</span>
                    {artifact.name ?? artifact.id}
                  </ArtifactLink>
                ) : (
                  <span key={index} className="gate-artifact-chip">
                    <span className="kind-chip">{artifact.kind ?? 'artifact'}</span>
                    {artifact.name ?? 'artifact'}
                  </span>
                ),
              )}
            </div>
          </div>
        )}

        <DialogueThread
          entries={payload.dialogue_tail}
          message={draft.message ?? ''}
          composerEnabled={payload.actions.includes('discuss')}
          disabled={disabled}
          onMessageChange={(message) => onEdit({ message })}
        />
      </div>

      <div className="phase-review-sidebar">
        <section className="gate-info-card" aria-label="Evidence for this decision">
          <h4>Agent evidence</h4>
          {payload.evidence?.projection_truncated === true && <p role="note" className="tonal-card warning">
            Some agent evidence was omitted from this review preview because it exceeded the display limit.
          </p>}
          {payload.evidence ? <ReasoningTab threadId={threadId} phase={payload.phase} entry={payload.evidence} state={{}} /> : <p>Agent evidence is unavailable in this review preview. Reasoning, tool calls and prompt provenance may be available in the run details.</p>}
          {payload.evidence?.provenance && <p>Output source: {payload.evidence.provenance === 'llm' ? 'AI-generated' : payload.evidence.provenance.replaceAll('_', ' ')}</p>}
          {typeof payload.evidence?.model === 'string' && <p>Model: {payload.evidence.model}</p>}
        </section>
        {previewEntries.length > 0 && !compact && (
          <details className="gate-info-card" data-testid="gate-result-preview">
            <summary>Technical output</summary>
            <CodeViewer
              value={JSON.stringify(preview, null, 2)}
              language="json"
              ariaLabel="Result preview"
            />
          </details>
        )}


      </div>
    </div>
  )
}
