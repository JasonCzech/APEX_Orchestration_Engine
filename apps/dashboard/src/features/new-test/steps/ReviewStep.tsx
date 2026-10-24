/**
 * Step 6 — Review: a launch summary that reads top to bottom in wizard order,
 * with an edit action per section and a sticky readiness checklist beside it.
 * Work-item keys and document ids are resolved into context packets at launch.
 */
import { changesSincePreset } from '@/features/presets/presetModel'
import { useId, useState, type ReactNode } from 'react'
import { Link } from 'react-router'
import { PHASE_NAMES } from '@apex/pipeline-events'
import { CopyableId } from '@/components/ux/CopyableId'
import { useAnalysisConnections } from '@/api/hooks/useAnalysisConnections'
import { useApplications, useEnvironments } from '@/api/hooks/useCatalog'
import { useAssistants } from '@/api/hooks/useAssistants'
import { usePromptList, usePromptVersion } from '@/api/hooks/usePrompts'
import { useWorkItem } from '@/api/hooks/useWorkTracking'
import type { PromptSelection } from '@/features/prompts/promptCategories'
import { PHASE_LABELS } from '@/features/runs/runDisplay'

import type { StepProps } from '../NewRunWizard'
import { parseStatusBadge, summarizeContext } from '../contextFiles'
import { reviewPolicySummary } from '../reviewPolicySummary'
import { parseTestScope, type TestParticipant } from '../testScope'
import { useWizardReadiness } from '../useWizardReadiness'
import { documentThresholdMismatches } from '../workload'
import {
  ANALYSIS_LABELS,
  buildLaunchPreview,
  gateMatrixOf,
  isRecord,
  issueFieldId,
  selectedPhases,
  STEP_LABELS,
  type WizardStepId,
  type WizardWorkItemRef,
} from '../wizardState'
import { promptSlotTarget } from './promptSlots'
import { WorkloadPreview } from './WorkloadFields'

const ENGINE_LABELS = { sim: 'Simulated', apex_load: 'APEX Load', loadrunner: 'LoadRunner' } as const
const LAYER_LABELS: Record<TestParticipant['layer'], string> = { frontend: 'Frontend', middle: 'Middle tier', backend: 'Backend' }
const LONG_REQUEST = 240

type SectionStatus = { tone: 'ok' | 'attention' | 'muted'; text: string }

function ReviewSection({ title, step, status, onEdit, children }: {
  title: string
  step: WizardStepId
  status?: SectionStatus
  onEdit: (step: WizardStepId) => void
  children: ReactNode
}) {
  const headingId = useId()
  return (
    <section className="wizard-review-section" aria-labelledby={headingId}>
      <header className="wizard-review-section-head">
        <h3 id={headingId} className="wizard-review-section-title">{title}</h3>
        {status && <span className={`wizard-review-status wizard-review-status--${status.tone}`}>{status.text}</span>}
        <button type="button" className="btn btn-ghost btn-sm wizard-review-edit" onClick={() => onEdit(step)} aria-label={`Edit ${title}`}>Edit</button>
      </header>
      <div className="wizard-review-section-body">{children}</div>
    </section>
  )
}

function ReferenceName({ name, id }: { name?: string; id: string | null | undefined }) {
  return (
    <span>
      {name ?? (id ? 'Name unavailable' : 'None')}
      {id && <small className="review-reference-id"><CopyableId value={id} label="Reference ID" /></small>}
    </span>
  )
}

function ParticipantRow({ p, primary, traffic }: { p: TestParticipant; primary: boolean; traffic: boolean }) {
  const apps = useApplications(p.project_id)
  const envs = useEnvironments(p.application_id)
  return (
    <li className="wizard-review-row">
      <span className="wizard-review-layer">{LAYER_LABELS[p.layer]}</span>
      <span className="wizard-review-row-main">
        <strong>{apps.data?.find(a => a.id === p.application_id)?.name ?? p.application_id}</strong>
        <span className="wizard-caption">{envs.data?.find(e => e.id === p.environment_id)?.name ?? p.environment_id} · {p.project_id}</span>
      </span>
      <span className="wizard-review-tags">
        {primary && <span className="wizard-review-tag">Primary system</span>}
        {traffic && <span className="wizard-review-tag">Traffic entry</span>}
      </span>
    </li>
  )
}

function WorkItemRow({ item, project }: { item: WizardWorkItemRef; project: string }) {
  const canLoad = Boolean(item.connection_id && item.provider)
  const query = useWorkItem(canLoad ? item.key : undefined, project, item.connection_id ?? undefined, item.provider ?? undefined)
  return (
    <li className="wizard-review-row">
      <code className="wizard-review-key">{item.key}</code>
      <span className="wizard-review-row-main">{query.data?.title ?? (canLoad && query.isPending ? 'Loading ticket…' : 'Title unavailable')}</span>
      {!canLoad && <span className="wizard-review-tag wizard-review-tag--warning">Revalidation required</span>}
    </li>
  )
}

function PromptRow({ slot, selection, note, onOpen }: { slot: string; selection: PromptSelection | null | undefined; note?: string; onOpen?: () => void }) {
  const catalog = usePromptList({ selection_enabled: true })
  const row = catalog.data?.find(row => row.id === selection?.prompt_id)
  const version = usePromptVersion(row?.namespace ?? '', row?.key ?? '', selection?.version_id, selection?.prompt_id)
  return (
    <tr>
      <th scope="row">{slot}</th>
      <td>
        <span className="wizard-review-cell">
          {note
            ? <span className="wizard-review-muted">{note}</span>
            : <button type="button" className="wizard-review-link" onClick={onOpen}>
              {row?.key ?? 'Not selected'}{selection ? ` · v${version.data?.version ?? selection.version_id}` : ''}
            </button>}
          {!note && selection?.override_content !== undefined && <span className="wizard-review-tag">Test override</span>}
        </span>
      </td>
    </tr>
  )
}

function RequestText({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false)
  const long = text.length > LONG_REQUEST
  return (
    <div className="wizard-review-request">
      <p className={long && !expanded ? 'wizard-review-clamp' : undefined}>{text || 'No request entered.'}</p>
      {long && <button type="button" className="btn btn-ghost btn-sm" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>{expanded ? 'Show less' : 'Show full request'}</button>}
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
  const traffic = draft.scope.test_scope?.participants.find(p => p.id === draft.scope.test_scope?.traffic_entry_participant_id)
  const environments = useEnvironments(traffic?.application_id ?? draft.scope.app_id)
  const configurations = useAssistants()
  const releaseConfig = draft.config.golden_configurable?.load_test as { release_id?: string; mapping_revision?: string } | undefined
  const { documents, readiness, environment: targetEnvironment, execution, executionVerificationEnabled } = useWizardReadiness(draft, maxContextPackets)
  const issues = [...readiness.planningIssues, ...readiness.executionIssues]
  const preview = buildLaunchPreview(draft)
  const analysisConnections = useAnalysisConnections(draft.scope.project_id, draft.config.engine !== 'sim')
  const phases = selectedPhases(draft.config)
  const gates = gateMatrixOf(draft.config)
  const scope = parseTestScope(draft.scope.test_scope)
  const inherited =
    draft.config.golden_configurable && isRecord(draft.config.golden_configurable['prompt_overrides'])
      ? draft.config.golden_configurable['prompt_overrides']
      : {}
  const overrides = Array.from(
    new Set([...Object.keys(inherited), ...Object.keys(draft.prompt_overrides)]),
  ).filter((key) => !draft.prompt_override_removals.includes(key))

  const knownDocs = new Map((documents.data ?? []).map((doc) => [doc.id, doc]))
  const contextSummary = summarizeContext(draft.document_ids.map((id) => knownDocs.get(id)))
  // LoadRunner applies its native test's SLAs, so Config thresholds don't apply.
  const thresholdMismatches = draft.config.engine === 'loadrunner' ? [] : documentThresholdMismatches(
    draft.document_ids.flatMap((id) => {
      const doc = knownDocs.get(id)
      return doc ? [{ name: doc.name ?? id, text: doc.text_preview }] : []
    }),
    draft.config.golden_configurable?.load_test,
  )

  function statusFor(step: WizardStepId, { optional = false, items = 0, extra = 0 } = {}): SectionStatus {
    const count = issues.filter(issue => issue.step === step).length + extra
    if (count) return { tone: 'attention', text: `${count} to fix` }
    if (optional && !items) return { tone: 'muted', text: 'Optional' }
    return { tone: 'ok', text: optional ? `${items} linked` : 'Complete' }
  }
  const openPrompt = (slot: Parameters<typeof promptSlotTarget>[0]) => () => onEditStep('prompts', promptSlotTarget(slot))
  const engineLabel = draft.config.unsupported_engine ? `${draft.config.unsupported_engine} (unsupported)` : ENGINE_LABELS[draft.config.engine]
  const appName = applications.data?.find((item) => item.id === draft.scope.app_id)?.name
  const envName = environments.data?.find((item) => item.id === draft.scope.environment_id)?.name
  const presetName = draft.preset?.name ?? configurations.data?.find((item) => item.assistantId === draft.config.golden_config_id)?.name
  const presetChanges = changesSincePreset(draft)
  const presetVersion = draft.preset?.version ? ` · v${draft.preset.version}` : ''

  return (
    <section className="wizard-step wizard-review" aria-label="Review">
      <div className="wizard-review-layout">
        <aside className="wizard-review-aside" aria-label="Launch readiness">
          <div className={`glass-panel wizard-review-checklist${issues.length ? ' wizard-review-checklist--attention' : ''}`}>
            <h3 className="sr-only">Launch readiness</h3>
            <p className="wizard-review-verdict">
              <span className="wizard-review-verdict-mark" aria-hidden="true">{issues.length ? '!' : '✓'}</span>
              {issues.length ? `${issues.length} ${issues.length === 1 ? 'item needs' : 'items need'} attention` : 'Ready to launch'}
            </p>
            {issues.length > 0 && (
              <div className="wizard-issues wizard-review-issues" role="alert" data-testid="review-issues">
                <ul>
                  {issues.map((issue) => (
                    <li key={`${issue.step}:${issue.message}`}>
                      <button type="button" className="wizard-review-issue" onClick={() => onEditStep(issue.step, issueFieldId(issue.message))}>
                        <span className="wizard-review-issue-step">{STEP_LABELS[issue.step]}:</span>{' '}
                        <span>{issue.message}</span>
                        <span className="wizard-review-issue-go" aria-hidden="true">Fix →</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <ul className="wizard-review-checks" aria-label="Launch checks">
              <li className={readiness.planningIssues.length ? 'is-attention' : 'is-ok'}>
                <strong>Test plan</strong>
                <span>{readiness.planningIssues.length === 0 ? 'Test plan is complete.' : 'Complete the outstanding planning items before launch.'}</span>
              </li>
              <li className={readiness.executionIssues.length ? 'is-attention' : 'is-ok'}>
                <strong>Execution configuration</strong>
                <span>{readiness.executionIssues.length === 0
                  ? readiness.executionNotes[0] ?? 'Selected target is approved; the server rechecks target and engine configuration at launch.'
                  : 'Needs attention before launch.'}</span>
              </li>
            </ul>
            {readiness.executionNotes.slice(readiness.executionIssues.length === 0 ? 1 : 0).map((note) => (
              <p key={note} className="wizard-caption">{note}</p>
            ))}
            {executionVerificationEnabled && (execution.isError || execution.data?.configuration_valid === false) && <button type="button" className="btn btn-secondary btn-sm" disabled={execution.isFetching} onClick={() => void execution.refetch()}>Retry execution verification</button>}
            {draft.scope.environment_id && readiness.executionIssues.length > 0 && <p className="wizard-caption">
              <Link to={`/environments/${encodeURIComponent(draft.scope.environment_id)}`}>Review environment target</Link>
              {targetEnvironment.isError && <button type="button" className="btn btn-secondary btn-sm" onClick={() => void targetEnvironment.refetch()}>Retry environment verification</button>}
            </p>}
            <p className="wizard-caption">Launching starts the test. Selected tickets and documents are resolved into agent context at that point.</p>
          </div>
        </aside>
        <div className="wizard-review-main">
          <header className="glass-panel wizard-review-hero">
            <span className="wizard-section-kicker">Test to launch</span>
            <h3 className="wizard-review-title">{draft.title.trim() || 'Untitled test'}</h3>
            <RequestText text={draft.request.trim()} />
            <dl className="wizard-review-facts">
              <div><dt>Engine</dt><dd>{engineLabel}</dd></div>
              <div><dt>Project</dt><dd>{draft.scope.project_id.trim() || '—'}</dd></div>
              <div><dt>Agent phases</dt><dd>{phases.length} of {PHASE_NAMES.length}</dd></div>
              <div><dt>Review policy</dt><dd>{draft.config.gates_mode === 'all_auto' ? 'Run automatically' : draft.config.gates_mode === 'custom' ? 'Custom' : 'Review every agent phase'}</dd></div>
            </dl>
            {draft.preset && (
              <p className="wizard-review-preset" data-testid="review-preset">
                Started from preset <strong>{draft.preset.name}</strong>{presetVersion}
                {presetChanges.length
                  ? <> · changed since: {presetChanges.map(change => change.field.label).join(', ')}</>
                  : <> · no changes</>}
              </p>
            )}
          </header>

          <div className="glass-panel wizard-review-sections">
            <ReviewSection title="Scope" step="scope" status={statusFor('scope')} onEdit={onEditStep}>
              {scope
                ? <ul className="wizard-review-list" aria-label="Systems under test">
                  {[...scope.participants].sort((a, b) => ['frontend', 'middle', 'backend'].indexOf(a.layer) - ['frontend', 'middle', 'backend'].indexOf(b.layer)).map(p =>
                    <ParticipantRow key={p.id} p={p} primary={p.id === scope.primary_participant_id} traffic={p.id === scope.traffic_entry_participant_id} />)}
                </ul>
                : <p className="wizard-review-muted">Detailed topology was not recorded.</p>}
              <dl className="wizard-review-dl">
                <dt>Application</dt>
                <dd><ReferenceName id={draft.scope.app_id} name={appName} /></dd>
                <dt>Environment</dt>
                <dd><ReferenceName id={draft.scope.environment_id} name={envName} /></dd>
              </dl>
            </ReviewSection>

            <ReviewSection title="Config" step="config" status={statusFor('config')} onEdit={onEditStep}>
              <ol className="wizard-review-phases" aria-label="Phase plan">
                {PHASE_NAMES.map((phase, index) => {
                  const agent = phases.includes(phase)
                  const reviews = [gates[phase].prompt_review === 'gated' && 'prompt', gates[phase].output_review === 'gated' && 'output'].filter(Boolean)
                  const owner = agent ? 'Agent' : 'Person'
                  const gateText = !agent ? 'Waits for a person' : reviews.length ? `Review: ${reviews.join(' + ')}` : 'No review pause'
                  return (
                    <li key={phase}>
                      {/* The name carries every visible word, in order, plus what bypassed means. */}
                      <button type="button" className={`wizard-review-phase${agent ? '' : ' wizard-review-phase--person'}`} onClick={openPrompt(`phase:${phase}`)}
                        aria-label={`${PHASE_LABELS[phase]}: ${owner}${agent ? '' : ', bypassed for agentic execution'}, ${gateText}. Opens it in Prompts`}>
                        <span className="wizard-review-phase-num" aria-hidden="true">{index + 1}</span>
                        <span className="wizard-review-phase-name">{PHASE_LABELS[phase]}</span>
                        <span className="wizard-review-phase-owner">{owner}</span>
                        <span className="wizard-review-phase-gates">{gateText}</span>
                      </button>
                    </li>
                  )
                })}
              </ol>
              <dl className="wizard-review-dl">
                {draft.config.engine === 'apex_load' && releaseConfig?.release_id && <>
                  <dt>Load release</dt><dd>{releaseConfig.release_id} · mapping {releaseConfig.mapping_revision ?? 'Not recorded'}</dd>
                </>}
                <dt>Analysis tools</dt><dd>{draft.config.analysis?.selections.map(s => `${ANALYSIS_LABELS[s.tool]} (${analysisConnections.data?.find(c => c.id === s.connection_id)?.name ?? s.connection_id})`).join(', ') || 'None'}</dd>
                <dt>Review policy</dt>
                <dd>{reviewPolicySummary(draft.config)}</dd>
                <dt>Test preset</dt>
                <dd>{draft.preset ? `${draft.preset.name}${presetVersion}` : <ReferenceName id={draft.config.golden_config_id} name={presetName} />}</dd>
              </dl>
            </ReviewSection>

            <ReviewSection title="Workload and acceptance" step="config" onEdit={onEditStep}>
              <div className="wizard-review-workload"><WorkloadPreview draft={draft} /></div>
              <p className="wizard-caption">Requested inputs, not measured results. Thresholds are evaluated after collection; they do not automatically stop a running test.</p>
            </ReviewSection>

            <ReviewSection title="Tickets" step="work-items" status={statusFor('work-items', { optional: true, items: draft.work_items.length })} onEdit={onEditStep}>
              {draft.work_items.length === 0
                ? <p className="wizard-review-muted">None linked. Tickets are optional context for the agents.</p>
                : <ul className="wizard-review-list">{draft.work_items.map((item) => <WorkItemRow key={`${item.connection_id}:${item.key}`} item={item} project={draft.scope.project_id} />)}</ul>}
            </ReviewSection>

            <ReviewSection title="Knowledge" step="context" status={statusFor('context', { optional: true, items: draft.document_ids.length, extra: thresholdMismatches.length ? 1 : 0 })} onEdit={onEditStep}>
              {thresholdMismatches.length > 0 && <div className="tonal-card warning" data-testid="document-threshold-mismatches">
                <p>Attached documents mention figures that differ from the acceptance thresholds in Config. Check which is the target:</p>
                <ul className="wizard-review-list">{thresholdMismatches.map(item => <li className="wizard-review-row" key={`${item.document}:${item.key}`}>{item.document} mentions {item.label} {item.documented}; Config requires ≤ {item.configured}.</li>)}</ul>
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => onEditStep('config', `workload-${thresholdMismatches[0]!.key}`)}>Review thresholds</button>
              </div>}
              {draft.document_ids.length === 0
                ? <p className="wizard-review-muted">No documents attached. Documents are optional context for the agents.</p>
                : <ul className="wizard-review-list">{draft.document_ids.map((id) => {
                  const doc = knownDocs.get(id)
                  const status = parseStatusBadge(doc?.parse_status)
                  return (
                    <li key={id} className="wizard-review-row">
                      <span className="wizard-review-row-main"><ReferenceName id={id} name={doc?.name} /></span>
                      <span className={`wizard-review-tag${doc && status.included ? '' : ' wizard-review-tag--warning'}`}>{doc ? `${status.label} · ${status.included ? 'Included' : 'Text not included'}` : 'Inclusion not verified'}</span>
                      {!doc && documents.data && <button type="button" className="btn btn-secondary btn-sm" onClick={() => onChange((previous) => ({ ...previous, document_ids: previous.document_ids.filter((item) => item !== id) }))}>Remove missing document</button>}
                    </li>
                  )
                })}</ul>}
              {contextSummary.includedCount > 0 && (
                <p className="wizard-caption">
                  {contextSummary.includedCount} parsed · ~{contextSummary.totalChars.toLocaleString()} characters included as context
                </p>
              )}
              {contextSummary.unreadableCount > 0 && (
                <p className="wizard-caption wizard-caption--warning">
                  {contextSummary.unreadableCount} file{contextSummary.unreadableCount === 1 ? '' : 's'} couldn’t be read and won’t be used as context
                </p>
              )}
              {documents.isError && draft.document_ids.length > 0 && <button type="button" className="btn btn-secondary btn-sm" onClick={() => void documents.refetch()}>Retry document verification</button>}
            </ReviewSection>

            <ReviewSection title="Prompts" step="prompts" status={statusFor('prompts')} onEdit={onEditStep}>
              {draft.prompt_selections ? (
                <table className="wizard-review-table">
                  <caption className="sr-only">Selected prompts, in the order they are listed on the Prompts step</caption>
                  <tbody>
                    <PromptRow slot="System prompt" selection={draft.prompt_selections.system} onOpen={openPrompt('system')} />
                    {draft.prompt_selections.application.length
                      ? draft.prompt_selections.application.map(item => <PromptRow key={item.prompt_id} slot="Application context" selection={item} onOpen={openPrompt('application')} />)
                      : <PromptRow slot="Application context" selection={null} note="None" />}
                    {draft.prompt_selections.environment.length
                      ? draft.prompt_selections.environment.map(item => <PromptRow key={item.prompt_id} slot="Environment context" selection={item} onOpen={openPrompt('environment')} />)
                      : <PromptRow slot="Environment context" selection={null} note="None" />}
                    {PHASE_NAMES.map(phase => <PromptRow key={phase} slot={`Phase context — ${PHASE_LABELS[phase]}`} selection={draft.prompt_selections?.phases[phase]}
                      note={phases.includes(phase) ? undefined : 'Bypassed: a person completes this phase'} onOpen={openPrompt(`phase:${phase}`)} />)}
                  </tbody>
                </table>
              ) : overrides.length === 0 ? (
                <p className="wizard-review-muted">Catalog prompts (no run overrides)</p>
              ) : (
                <div className="wizard-chip-row">
                  {overrides.map((key) => <span key={key} className="wizard-chip">{key} · run override</span>)}
                </div>
              )}
            </ReviewSection>
          </div>

          <details className="glass-panel wizard-accordion">
            <summary className="wizard-accordion-summary">Technical details: launch JSON</summary>
            <pre className="wizard-json" data-testid="launch-payload-json">
              {JSON.stringify(preview, null, 2)}
            </pre>
          </details>
        </div>

      </div>
    </section>
  )
}
