/**
 * phase_review gate panel (plan 2.a): summary block, result_preview JSON-ish
 * view, artifact chips (linking into the run's artifact viewer), warning
 * cards, and the DialogueThread (dialogue_tail + discuss composer).
 */
import { Link } from 'react-router'

import type { PhaseReviewPayload } from '@apex/pipeline-events'

import { CodeViewer } from '@/components/viewers/CodeViewer'

import { DialogueThread } from './DialogueThread'
import type { GateDraft, GateDraftPatch } from './gateMachine'

export function PhaseReviewPanel({
  threadId,
  payload,
  draft,
  disabled,
  compact = false,
  onEdit,
}: {
  threadId: string
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
    <div className="gate-panel" data-testid="phase-review-panel">
      {paragraphs.length > 0 ? (
        <div className="gate-summary" data-testid="gate-summary">
          {paragraphs.map((paragraph, index) => (
            <p key={index}>{paragraph}</p>
          ))}
        </div>
      ) : (
        <div className="dash-empty small">No summary provided for this phase result.</div>
      )}

      {payload.warnings.map((warning, index) => (
        <div key={index} className="tonal-card warning" data-testid="gate-warning">
          {warning}
        </div>
      ))}

      {previewEntries.length > 0 && !compact && (
        <details className="gate-collapsible" data-testid="gate-result-preview">
          <summary>Result preview</summary>
          <CodeViewer
            value={JSON.stringify(preview, null, 2)}
            language="json"
            ariaLabel="Result preview"
          />
        </details>
      )}

      {payload.artifacts.length > 0 && (
        <div className="gate-artifacts" data-testid="gate-artifacts">
          <h4 className="gate-field-label">Artifacts</h4>
          <div className="gate-chip-row">
            {payload.artifacts.map((artifact, index) =>
              artifact.id ? (
                <Link
                  key={artifact.id}
                  className="gate-artifact-chip"
                  to={`/runs/${threadId}/artifacts/${artifact.id}`}
                >
                  <span className="kind-chip">{artifact.kind ?? 'artifact'}</span>
                  {artifact.name ?? artifact.id}
                </Link>
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
  )
}
