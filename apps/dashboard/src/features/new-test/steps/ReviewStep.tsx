import { reviewPolicySummary } from '../reviewPolicySummary'
/**
 * Step 6 — Review: summary cards with edit-links back to each step,
 * planning and execution readiness, plus the collapsible launch plan. Work-item
 * keys and document ids are resolved into context packets at launch time.
 */
import type { ReactNode } from 'react'
import { CopyableId } from '@/components/ux/CopyableId'

import { useWizardReadiness } from '../useWizardReadiness'
import { WorkloadPreview } from './WorkloadFields'
import { Link } from 'react-router'

import { useApplications, useEnvironments } from '@/api/hooks/useCatalog'
import { useAssistants } from '@/api/hooks/useAssistants'
import { useWorkItem } from '@/api/hooks/useWorkTracking'
import { PHASE_LABELS } from '@/features/runs/runDisplay'
import { parseStatusBadge, summarizeContext } from '../contextFiles'
import type { StepProps } from '../NewRunWizard'
import {
  issueFieldId,
  buildLaunchPreview,
  isRecord,
  selectedPhases,
  STEP_LABELS,
  type WizardStepId,
} from '../wizardState'

function ReferenceName({ name, id }: { name?: string; id: string | null | undefined }) {
  return (
    <span>
      {name ?? (id ? 'Name unavailable' : 'None')}
      {id && <small className="review-reference-id"><CopyableId value={id} label="Reference ID" /></small>}
    </span>
  )
}

function WorkItemReview({ item, project }: { item: import('../wizardState').WizardWorkItemRef; project: string }) {
  const canLoad = Boolean(item.connection_id && item.provider)
  const query = useWorkItem(canLoad ? item.key : undefined, project, item.connection_id ?? undefined, item.provider ?? undefined)
  return <li>{query.data?.title ?? (canLoad && query.isPending ? 'Loading ticket…' : 'Title unavailable')} · <code>{item.key}</code>{!canLoad ? ' · Revalidation required' : ''}</li>
}

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
  onChange,
  onEditStep,
  maxContextPackets,
}: Pick<StepProps, 'draft' | 'maxContextPackets' | 'onChange'> & {
  onEditStep: (step: WizardStepId, field?: string) => void
}) {
  const applications = useApplications(draft.scope.project_id || undefined)
  const environments = useEnvironments(draft.scope.app_id)
  const configurations = useAssistants()
  const releaseConfig = draft.config.golden_configurable?.load_test as { release_id?: string; mapping_revision?: string } | undefined
  const { documents, readiness, environment: targetEnvironment, execution, executionVerificationEnabled } = useWizardReadiness(draft, maxContextPackets)
  const issues = [...readiness.planningIssues, ...readiness.executionIssues]
  const preview = buildLaunchPreview(draft)
  const phases = selectedPhases(draft.config)
  const inherited =
    draft.config.golden_configurable && isRecord(draft.config.golden_configurable['prompt_overrides'])
      ? draft.config.golden_configurable['prompt_overrides']
      : {}
  const overrides = Array.from(
    new Set([...Object.keys(inherited), ...Object.keys(draft.prompt_overrides)]),
  ).filter((key) => !draft.prompt_override_removals.includes(key))

  const knownDocs = new Map((documents.data ?? []).map((doc) => [doc.id, doc]))
  const contextSummary = summarizeContext(draft.document_ids.map((id) => knownDocs.get(id)))

  return (
    <section className="wizard-step" aria-label="Review">
      {documents.isError && draft.document_ids.length > 0 && <button type="button" className="btn btn-secondary" onClick={() => void documents.refetch()}>Retry document verification</button>}
      {overrides.some((key) => key.startsWith('phase/') && !phases.includes(key.split('/')[1] as typeof phases[number])) && <p className="tonal-card warning">Overrides for excluded phases are saved in this draft and will not be sent at launch.</p>}
      {issues.length > 0 && (
        <div className="wizard-issues" role="alert" data-testid="review-issues">
          <span className="wizard-label">Outstanding issues</span>
          <ul>
            {issues.map((issue) => (
              <li key={`${issue.step}:${issue.message}`}>
                <button
                  type="button"
                  className="wizard-issue-link"
                  onClick={() => onEditStep(issue.step, issueFieldId(issue.message))}
                >
                  {STEP_LABELS[issue.step]}: {issue.message}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="wizard-review-grid">
        <ReviewCard title="Readiness" step={readiness.executionIssues.length > 0 ? 'scope' : 'config'} onEdit={onEditStep}>
          {readiness.planningIssues.length === 0 ? (
            <p className="wizard-caption">Test plan is complete.</p>
          ) : (
            <p className="wizard-caption wizard-caption--warning">Complete the outstanding planning items before launch.</p>
          )}
          {readiness.executionIssues.length === 0 ? (
            <p className="wizard-caption">Execution configuration: {readiness.executionNotes[0] ?? 'Selected target is approved; the server rechecks target and engine configuration at launch.'}</p>
          ) : (
            <div>
              <p className="wizard-caption wizard-caption--danger">Execution configuration needs attention:</p>
              <ul>{readiness.executionIssues.map((issue) => <li key={issue.message}>{issue.message}</li>)}</ul>
            </div>
          )}
          {readiness.executionNotes.slice(readiness.executionIssues.length === 0 ? 1 : 0).map((note) => (
            <p key={note} className="wizard-caption">{note}</p>
          ))}
          {executionVerificationEnabled && (execution.isError || execution.data?.configuration_valid === false) && <button type="button" className="btn btn-secondary btn-sm" disabled={execution.isFetching} onClick={() => void execution.refetch()}>Retry execution verification</button>}
        </ReviewCard>
        {draft.scope.environment_id && readiness.executionIssues.length > 0 && <p className="wizard-caption"><Link to={`/environments/${encodeURIComponent(draft.scope.environment_id)}`}>Review environment target</Link>{targetEnvironment.isError && <button type="button" className="btn btn-secondary btn-sm" onClick={() => void targetEnvironment.refetch()}>Retry environment verification</button>}</p>}
        <ReviewCard title="Scope" step="scope" onEdit={onEditStep}>
          <dl className="wizard-review-dl">
            <dt>Title</dt>
            <dd>{draft.title.trim() || '—'}</dd>
            <dt>Request</dt>
            <dd className="wizard-review-clamp">{draft.request.trim() || '—'}</dd>
            <dt>Project</dt>
            <dd>{draft.scope.project_id.trim() || '—'}</dd>
            <dt>Application</dt>
            <dd><ReferenceName id={draft.scope.app_id} name={applications.data?.find((item) => item.id === draft.scope.app_id)?.name} /></dd>
            <dt>Environment</dt>
            <dd><ReferenceName id={draft.scope.environment_id} name={environments.data?.find((item) => item.id === draft.scope.environment_id)?.name} /></dd>
          </dl>
        </ReviewCard>

        <ReviewCard title="Tickets" step="work-items" onEdit={onEditStep}>
          {draft.work_items.length === 0 ? (
            <p className="wizard-caption">None linked (optional)</p>
          ) : (
            <ul>{draft.work_items.map((item) => <WorkItemReview key={`${item.connection_id}:${item.key}`} item={item} project={draft.scope.project_id} />)}</ul>
          )}
        </ReviewCard>

        <ReviewCard title="Knowledge" step="context" onEdit={onEditStep}>
          <p className="wizard-caption">
            {draft.document_ids.length} document{draft.document_ids.length === 1 ? '' : 's'}{' '}
            attached
          </p>
          <ul>{draft.document_ids.map((id) => {
            const doc = knownDocs.get(id)
            const status = parseStatusBadge(doc?.parse_status)
            return <li key={id}><ReferenceName id={id} name={doc?.name} /> — {doc ? `${status.label} · ${status.included ? 'Included' : 'Text not included'}` : 'Inclusion not verified'}{!doc && documents.data && <button type="button" className="btn btn-secondary btn-sm" onClick={() => onChange((previous) => ({ ...previous, document_ids: previous.document_ids.filter((item) => item !== id) }))}>Remove missing document</button>}</li>
          })}</ul>
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
            <dd>{draft.config.unsupported_engine ? `${draft.config.unsupported_engine} (unsupported)` : {sim:'Simulated',apex_load:'APEX Load',loadrunner:'LoadRunner'}[draft.config.engine]}</dd>
            {draft.config.engine === 'apex_load' && releaseConfig?.release_id && <>
              <dt>Load release</dt><dd>{releaseConfig.release_id} · mapping {releaseConfig.mapping_revision ?? 'Not recorded'}</dd>
            </>}
            <dt>Phases</dt>
            <dd>
              {phases.map((phase) => PHASE_LABELS[phase]).join(' → ') || 'None'}
            </dd>
            <dt>Review policy</dt>
            <dd>{reviewPolicySummary(draft.config)}</dd>
            <dt>Test preset</dt>
            <dd><ReferenceName id={draft.config.golden_config_id} name={configurations.data?.find((item) => item.assistantId === draft.config.golden_config_id)?.name} /></dd>
          </dl>
        </ReviewCard>

        <ReviewCard title="Workload and acceptance" step="config" onEdit={onEditStep}>
          <WorkloadPreview draft={draft} />
          <p className="wizard-caption">Requested inputs, not measured results. Thresholds are evaluated after collection; they do not automatically stop a running test.</p>
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
        <summary className="wizard-accordion-summary">Technical details: launch JSON</summary>
        <pre className="wizard-json" data-testid="launch-payload-json">
          {JSON.stringify(preview, null, 2)}
        </pre>
      </details>
    </section>
  )
}
