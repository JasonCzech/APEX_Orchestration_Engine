/**
 * Step 6 — Review: summary cards with edit-links back to each step,
 * outstanding validation issues, and the collapsible launch plan. Work-item
 * keys and document ids are resolved into context packets at launch time.
 */
import type { ReactNode } from 'react'

import { useDocumentsList } from '@/api/hooks/useDocuments'

import { summarizeContext } from '../contextFiles'
import type { StepProps } from '../NewRunWizard'
import {
  allIssues,
  buildLaunchPreview,
  selectedPhases,
  STEP_LABELS,
  type WizardStepId,
} from '../wizardState'

function ReviewCard({
  title,
  step,
  onEdit,
  children,
}: {
  title: string
  step: WizardStepId
  onEdit: (step: WizardStepId) => void
  children: ReactNode
}) {
  return (
    <div className="glass-panel wizard-review-card">
      <div className="wizard-review-card-head">
        <h3 className="wizard-review-card-title">{title}</h3>
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => onEdit(step)}
          aria-label={`Edit ${title}`}
        >
          Edit
        </button>
      </div>
      <div className="wizard-review-card-body">{children}</div>
    </div>
  )
}

export function ReviewStep({
  draft,
  onEditStep,
}: Pick<StepProps, 'draft'> & { onEditStep: (step: WizardStepId) => void }) {
  const issues = allIssues(draft)
  const preview = buildLaunchPreview(draft)
  const phases = selectedPhases(draft.config)
  const overrides = Object.keys(draft.prompt_overrides)

  const documents = useDocumentsList(draft.scope.project_id.trim() || undefined)
  const knownDocs = new Map((documents.data ?? []).map((doc) => [doc.id, doc]))
  const contextSummary = summarizeContext(draft.document_ids.map((id) => knownDocs.get(id)))

  return (
    <section className="wizard-step" aria-label="Review">
      {issues.length > 0 && (
        <div className="wizard-issues" role="alert" data-testid="review-issues">
          <span className="wizard-label">Outstanding issues</span>
          <ul>
            {issues.map((issue) => (
              <li key={`${issue.step}:${issue.message}`}>
                <button
                  type="button"
                  className="wizard-issue-link"
                  onClick={() => onEditStep(issue.step)}
                >
                  {STEP_LABELS[issue.step]}: {issue.message}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="wizard-review-grid">
        <ReviewCard title="Scope" step="scope" onEdit={onEditStep}>
          <dl className="wizard-review-dl">
            <dt>Title</dt>
            <dd>{draft.title.trim() || '—'}</dd>
            <dt>Request</dt>
            <dd className="wizard-review-clamp">{draft.request.trim() || '—'}</dd>
            <dt>Project</dt>
            <dd>{draft.scope.project_id.trim() || '—'}</dd>
            <dt>Application</dt>
            <dd>{draft.scope.app_id ?? '—'}</dd>
            <dt>Environment</dt>
            <dd>{draft.scope.environment_id ?? '—'}</dd>
          </dl>
        </ReviewCard>

        <ReviewCard title="Work items" step="work-items" onEdit={onEditStep}>
          {draft.work_item_keys.length === 0 ? (
            <p className="wizard-caption">None linked (optional)</p>
          ) : (
            <div className="wizard-chip-row">
              {draft.work_item_keys.map((key) => (
                <span key={key} className="wizard-chip">
                  {key}
                </span>
              ))}
            </div>
          )}
        </ReviewCard>

        <ReviewCard title="Context" step="context" onEdit={onEditStep}>
          <p className="wizard-caption">
            {draft.document_ids.length} document{draft.document_ids.length === 1 ? '' : 's'}{' '}
            attached
          </p>
          {contextSummary.includedCount > 0 && (
            <p className="wizard-caption">
              {contextSummary.includedCount} parsed · ~
              {contextSummary.totalChars.toLocaleString()} characters included as context
            </p>
          )}
          {contextSummary.unreadableCount > 0 && (
            <p className="wizard-caption wizard-caption--warning">
              {contextSummary.unreadableCount} file{contextSummary.unreadableCount === 1 ? '' : 's'}{' '}
              couldn’t be read and won’t be used as context
            </p>
          )}
        </ReviewCard>

        <ReviewCard title="Config" step="config" onEdit={onEditStep}>
          <dl className="wizard-review-dl">
            <dt>Engine</dt>
            <dd>{draft.config.engine}</dd>
            <dt>Phases</dt>
            <dd>
              {draft.config.phases === null
                ? 'all 7 (canonical order)'
                : phases.map((phase) => phase.replaceAll('_', ' ')).join(' → ') || 'none'}
            </dd>
            <dt>Gates</dt>
            <dd>{draft.config.gates_mode.replaceAll('_', ' ')}</dd>
            <dt>Golden config</dt>
            <dd>{draft.config.golden_config_id ?? '—'}</dd>
          </dl>
        </ReviewCard>

        <ReviewCard title="Prompts" step="prompts" onEdit={onEditStep}>
          {overrides.length === 0 ? (
            <p className="wizard-caption">Catalog prompts (no run overrides)</p>
          ) : (
            <div className="wizard-chip-row">
              {overrides.map((key) => (
                <span key={key} className="wizard-chip">
                  {key} · run override
                </span>
              ))}
            </div>
          )}
        </ReviewCard>
      </div>

      <details className="glass-panel wizard-accordion">
        <summary className="wizard-accordion-summary">Launch plan</summary>
        <pre className="wizard-json" data-testid="launch-payload-json">
          {JSON.stringify(preview, null, 2)}
        </pre>
      </details>
    </section>
  )
}
