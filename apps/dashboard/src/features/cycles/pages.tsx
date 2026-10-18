import { useTimestampPreference } from '@/theme/preferences'
import { StateCard } from '@/components/ux/StateCard'
import { Skeleton } from '@/components/ux/Skeleton'
import { CycleSources } from './CycleSources'
import { CycleHandoff } from './CycleHandoff'
import { FrozenEvidenceDiff } from './FrozenEvidenceDiff'
import { ReportEvidence } from './ReportEvidence'
import { CycleResults } from './CycleResults'
import { useEffect, useState, type FormEvent } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'
import { useAuth, useConsumer } from '@/auth/AuthProvider'
import { canLaunchPipelines, canMutateAudience, pipelineLaunchRefusal } from '@/auth/RequireRole'
import { useApplicationsIndex, useEnvironmentsIndex } from '@/api/hooks/useEnvironments'
import { useApplications, useEnvironments } from '@/api/hooks/useCatalog'
import { ProblemCard } from '@/components/ProblemCard'
import { Dialog } from '@/components/Dialog'
import { ProjectField } from '@/components/ux/ProjectField'
import { ProjectIdentity } from '@/components/ux/ProjectIdentity'
import { CycleActorIdentity, CycleActorNames } from './CycleActorIdentity'
import { CycleProgress } from './CycleProgress'
import { CopyableId } from '@/components/ux/CopyableId'
import { Pagination } from '@/components/ux/Pagination'
import { ApiError } from '@/api/errors'
import { formatTimestamp } from '@/utils/time'
import { PromptDiff } from '@/features/prompts/PromptDiff'
import { cycleNextAction, fieldValue, REVIEW_LABELS, REVIEW_EFFECTS, REVIEW_RESULTS } from './cycleDisplay'
import { ProviderScriptImport } from './ProviderScriptImport'
import { ScriptStorage } from './ScriptStorage'
import { ExecutionWorkspace } from './ExecutionWorkspace'
import { CycleEditor } from './CycleEditor'
import {
  useCycle,
  useExecutions,
  useCycles,
  useCreateCycle,
  useReview,
  type Cycle,
  type Version,
  type VersionWrite,
  type Review,
} from './api'
import './cycles.css'

const steps = [
  ['overview', 'Overview'],
  ['scope', 'Scope'],
  ['test_plan', 'Test plan'],
  ['readiness', 'Readiness'],
  ['script', 'Scripts'],
  ['executions', 'Executions'],
  ['report', 'Results & reports'],
  ['postmortem', 'Postmortem'],
] as const
function useEnabled() {
  const { state } = useAuth()
  return (
    state.status === 'authenticated' &&
    state.systemInfo.features.cycles === true
  )
}
function Disabled() {
  return (
    <StateCard kind="unconfigured" title="Test cycles" action={<Link className="btn btn-secondary" to="/runs">Open tests</Link>}>
      The cycle workspace is not enabled on this server.
    </StateCard>
  )
}

function NewCycle({ onCreated }: { onCreated: (id: string) => void }) {
  const consumer = useConsumer()
  const [name, setName] = useState('')
  const [project, setProject] = useState(consumer?.scopes[0]?.project_id ?? '')
  const [app, setApp] = useState('')
  const [environment, setEnvironment] = useState('')
  const [mode, setMode] = useState<'live' | 'simulation'>('live')
  const applications = useApplications(project)
  const environments = useEnvironments(app)
  const create = useCreateCycle()
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!create.isPending && canMutateAudience(consumer, project, app))
      create.mutate(
        {
          name,
          project_id: project,
          app_id: app,
          environment_id: environment,
          mode,
        },
        { onSuccess: (cycle) => onCreated(cycle.id) },
      )
  }
  return (
    <form
      onSubmit={submit}
      className="cycle-panel glass-panel"
      aria-label="New test cycle"
    >
      <h2>New test cycle</h2>
      <fieldset disabled={create.isPending} className="cycle-form-grid">
        <label className="cycle-field">
          Cycle name
          <input
            required
            maxLength={255}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <ProjectField required value={project} onChange={(value) => { setProject(value); setApp(''); setEnvironment('') }} />
        <label className="cycle-field">
          Application
          <select
            required
            value={app}
            onChange={(e) => {
              setApp(e.target.value)
              setEnvironment('')
            }}
          >
            <option value="">Select application</option>
            {applications.data?.map((a) => (
              <option value={a.id} key={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </label>
        <label className="cycle-field">
          Environment
          <select
            required
            value={environment}
            onChange={(e) => setEnvironment(e.target.value)}
          >
            <option value="">Select environment</option>
            {environments.data?.map((e) => (
              <option key={e.id} value={e.id}>
                {e.name}
              </option>
            ))}
          </select>
        </label>
        <label className="cycle-field">
          Execution mode
          <select
            value={mode}
            onChange={(e) => setMode(e.target.value as typeof mode)}
          >
            <option value="live">Live providers</option>
            <option value="simulation">Simulation — synthetic evidence</option>
          </select>
        </label>
        <button
          className="btn btn-primary"
          disabled={applications.isFetching || environments.isFetching || !canMutateAudience(consumer, project, app)}
          type="submit"
        >
          {create.isPending ? 'Creating…' : 'Create cycle'}
        </button>
      </fieldset>
      {project && app && !canMutateAudience(consumer, project, app) && <p className="tonal-card warning">This application is outside your authorized scope.</p>}
      {(create.error || applications.error || environments.error) && (
        <p role="alert">
          {(create.error || applications.error || environments.error)?.message}
        </p>
      )}
    </form>
  )
}

export function CyclesPage() {
  useTimestampPreference()
  const enabled = useEnabled()
  const consumer = useConsumer()
  const [offset, setOffset] = useState(0)
  const [limit, setLimit] = useState(20)
  const [newOpen, setNewOpen] = useState(false)
  const [created, setCreated] = useState('')
  const query = useCycles(offset, enabled, limit)
  const applications = useApplicationsIndex()
  const environments = useEnvironmentsIndex()
  if (!enabled) return <Disabled />
  return (
    <div className="cycle-workspace">
      <header className="cycle-heading">
        <div>
          <h2>Test cycles</h2>
          <p>
            Plan, review and maintain engineering deliverables across test
            attempts.
          </p>
        </div>
        {canLaunchPipelines(consumer) && (
          <button
            className="btn btn-primary"
            onClick={() => setNewOpen(!newOpen)}
          >
            {newOpen ? 'Close new cycle' : 'New cycle'}
          </button>
        )}
      </header>
      {!canLaunchPipelines(consumer) && <p className="tonal-card info">{pipelineLaunchRefusal(consumer)} You can view cycles in your authorized scope.</p>}
      {newOpen && (
        <NewCycle
          onCreated={(id) => {
            setCreated(id)
            setNewOpen(false)
          }}
        />
      )}
      {created && (
        <p role="status">
          Cycle created.{' '}
          <Link to={`/cycles/${created}`}>Open cycle workspace</Link>
        </p>
      )}
      {query.isPending && <Skeleton label="Loading cycles" rows={2} />}
      {query.error && (
        <ProblemCard
          title="Could not load cycles"
          message={query.error.message}
          onRetry={() => void query.refetch()}
        />
      )}
      <div className="cycle-card-grid">
        {query.data?.map((cycle) => (
          <article key={cycle.id} className="glass-panel cycle-panel">
            <h2>
              <Link to={`/cycles/${cycle.id}`}>{cycle.name}</Link>
            </h2>
            <p>
              <ProjectIdentity projectId={cycle.project_id} /> · {applications.data?.find(item => item.id === cycle.app_id)?.name ?? cycle.app_id} · {environments.data?.find(item => item.id === cycle.environment_id)?.name ?? cycle.environment_id} ·{' '}
              {cycle.mode === 'simulation' ? 'Simulation' : 'Live providers'}
            </p>
            <p>Owner: <CycleActorIdentity id={cycle.owner} name={cycle.owner_name} /></p>
            <CycleProgress progress={cycle.progress} />
            <small>
              Updated {formatTimestamp(cycle.updated_at)} · revision{' '}
              {cycle.revision}
            </small>
          </article>
        ))}
      </div>
      {query.data?.length === 0 && (
        <StateCard kind="empty" title="No cycles on this page." action={!newOpen && canLaunchPipelines(consumer) ? <button className="btn btn-primary" onClick={() => setNewOpen(true)}>Create cycle</button> : null}>
          {canLaunchPipelines(consumer) ? 'Create a cycle to assemble your first deliverables.' : 'Ask an administrator for access to a project’s cycles.'}
        </StateCard>
      )}
      <Pagination label="Cycles" options={[10, 20, 50, 100]} offset={offset} limit={limit} pending={query.isFetching} hasNext={query.data?.length === limit && offset + limit <= 10000}
        onChange={(page) => { setOffset(page.offset); setLimit(page.limit) }} />
    </div>
  )
}

function VersionDetails({
  cycle,
  version,
  onReviewed,
}: {
  cycle: Cycle
  version: Version
  onReviewed: () => void
}) {
  const review = useReview(cycle.id)
  const [note, setNote] = useState('')
  const [compare, setCompare] = useState('')
  const [reportView, setReportView] = useState('technical')
  const [showUnchanged, setShowUnchanged] = useState(false)
  const [confirmation, setConfirmation] = useState<Review | null>(null)
  const [applied, setApplied] = useState<Review['action'] | null>(null)
  const baseline = cycle.versions.find((v) => v.id === compare)
  const fields = cycle.fields?.[version.kind] ?? []
  const reviewed = version.status === 'approved'
  const latestReview = version.reviews.at(-1)
  const unchanged = fields.filter((field) => baseline && JSON.stringify(baseline.content[field.key]) === JSON.stringify(version.content[field.key])).length
  return (
    <section
      className="cycle-panel glass-panel"
      aria-label="Selected deliverable version"
    >
      <h2>{version.title}</h2>
      <p>
        <strong>
          {version.archived ? 'Archived' : version.status.replaceAll('_', ' ')}
        </strong>
        {reviewed && !version.approval_current ? ' · Approval is stale' : ''} ·
        version <CopyableId value={version.id} label="Version ID" />
      </p>
      <p>{version.change_summary}</p>
      {latestReview?.action === 'request_revision' && <div className="tonal-card warning"><strong>Revision requested</strong><p>{latestReview.note || 'No review note was provided.'}</p></div>}
      {version.origin && (
        <p>
          Reused from{' '}
          <Link
            to={`/cycles/${encodeURIComponent(version.origin.cycle_id ?? '')}?tab=${encodeURIComponent(version.kind)}&version=${encodeURIComponent(version.origin.version_id ?? '')}`}
          >
            the original deliverable version
          </Link>
          ; new approvals are required.
        </p>
      )}
      <p>
        Created by <CycleActorIdentity id={version.created_by} /> ·{' '}
        {formatTimestamp(version.created_at)}
      </p>
      {version.kind === 'report' && (
        <>
          <p>
            Publication is not configured. Approval stores this exact version in
            APEX; it does not publish a page or create defects.
          </p>
          <label className="cycle-field">
            Report view
            <select
              value={reportView}
              onChange={(e) => setReportView(e.target.value)}
            >
              <option value="technical">Technical report</option>
              <option value="executive">Executive report</option>
            </select>
          </label>
        </>
      )}
      <label className="cycle-field">
        Compare with version
        <select value={compare} onChange={(e) => setCompare(e.target.value)}>
          <option value="">No comparison</option>
          {cycle.versions
            .filter((v) => v.kind === version.kind && v.id !== version.id)
            .map((v) => (
              <option value={v.id} key={v.id}>
                {v.title} · {v.id} · {v.status}
              </option>
            ))}
        </select>
      </label>
      {baseline && unchanged > 0 && <label><input type="checkbox" checked={showUnchanged} onChange={(event) => setShowUnchanged(event.target.checked)} /> Show unchanged ({unchanged})</label>}
      {version.kind === 'report' && baseline && (
        <FrozenEvidenceDiff
          selected={version.content._execution_evidence}
          baseline={baseline.content._execution_evidence}
        />
      )}
      <div className={baseline ? 'cycle-diff' : ''}>
        {fields
          .filter((field) => !baseline || showUnchanged || JSON.stringify(baseline.content[field.key]) !== JSON.stringify(version.content[field.key]))
          .filter(
            (f) =>
              version.kind !== 'report' ||
              reportView === 'technical' ||
              [
                'executive_summary',
                'recommendation',
                'accepted_risks',
                'evidence',
              ].includes(f.key),
          )
          .map((field) => (
            <section key={field.key} className="cycle-section">
              <h3>
                {field.label}
                {baseline &&
                JSON.stringify(baseline.content[field.key]) !==
                  JSON.stringify(version.content[field.key])
                  ? ' · Changed'
                  : ''}
              </h3>
              {baseline && field.control === 'multiline' ? <PromptDiff original={fieldValue(baseline.content[field.key])} value={fieldValue(version.content[field.key])} ariaLabel={`${field.label} changes`} /> : <>
              {baseline && (
                <div>
                  <strong>Comparison version</strong>
                  <p>{fieldValue(baseline.content[field.key])}</p>
                </div>
              )}
              <div>
                {baseline && <strong>Selected version</strong>}
                <p>{fieldValue(version.content[field.key])}</p>
              </div>
              </>}
            </section>
          ))}
      </div>
      {version.validation_issues.length > 0 && (
        <div className="tonal-card warning">
          <h3>Before review</h3>
          <ul>
            {version.validation_issues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        </div>
      )}
      <h3>Frozen upstream versions</h3>
      <ul>
        {version.dependencies.map((id) => {
          const upstream = cycle.versions.find((v) => v.id === id)
          return (
            <li key={id}>
              {upstream?.title ?? 'Missing upstream version'} · <CopyableId value={id} label="Upstream version ID" />{' '}
              ·{' '}
              {upstream?.approval_current
                ? 'Approval current'
                : 'Approval not current'}
            </li>
          )
        })}
      </ul>
      {!version.dependencies.length && <p>No upstream versions selected.</p>}
      <details>
        <summary>Version identity and review history</summary>
        <p>Version ID: <CopyableId value={version.id} label="Version ID" /></p>
        <p>Content SHA-256: {version.checksum}</p>
        <ul>
          {version.reviews.map((r, i) => (
            <li key={i}>
              {REVIEW_RESULTS[r.action as Review['action']] ?? r.action} · <CycleActorIdentity id={r.actor} /> · {formatTimestamp(r.at)}
              <p>{r.note}</p>
            </li>
          ))}
        </ul>
      </details>
      {version.available_actions.length > 0 && (
        <>
          <label className="cycle-field">
            Review note
            <textarea
              maxLength={2000}
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </label>
          <div className="cycle-actions">
            {[...version.available_actions].sort((a, b) => Number(a === 'archive') - Number(b === 'archive')).map((action) => (
              <button
                type="button"
                className={
                  action === 'approve' ? 'btn btn-primary' : action === 'archive' ? 'btn btn-danger' : 'btn btn-secondary'
                }
                disabled={review.isPending}
                key={action}
                onClick={() =>
                  setConfirmation({
                    action: action as Review['action'],
                    version_id: version.id,
                    expected_revision: cycle.revision,
                    note,
                  })
                }
              >
                {REVIEW_LABELS[action as Review['action']] ?? action}
              </button>
            ))}
          </div>
        </>
      )}
      {confirmation && <Dialog ariaLabel={REVIEW_LABELS[confirmation.action]} className="cycle-leave-dialog" overlayClassName="cycle-dialog-overlay" onClose={() => { if (!review.isPending) setConfirmation(null) }} closeOnEscape={!review.isPending} closeOnBackdrop={!review.isPending}>
        <h2>{REVIEW_LABELS[confirmation.action]}</h2>
        <p><strong>{version.title}</strong></p>
        <p>{REVIEW_EFFECTS[confirmation.action]}</p>
        <p>Version {confirmation.version_id} · cycle revision {confirmation.expected_revision}</p>
        {confirmation.note && <p>Review note: {confirmation.note}</p>}
        {review.error && <p role="alert" className="tonal-card danger">{review.error.message}</p>}
        <div className="ux-dialog-actions">
          <button className="btn btn-secondary" disabled={review.isPending} onClick={() => setConfirmation(null)}>Cancel</button>
          <button className={confirmation.action === 'archive' ? 'btn btn-danger' : 'btn btn-primary'} disabled={review.isPending} onClick={() => review.mutate(confirmation, { onSuccess: () => { setApplied(confirmation.action); setNote(''); setConfirmation(null); onReviewed() } })}>{review.isPending ? 'Saving…' : `Confirm ${REVIEW_LABELS[confirmation.action].toLowerCase()}`}</button>
        </div>
      </Dialog>}
      {review.error && !confirmation && (
        <p role="alert">
          {review.error.message} Refresh the cycle and review the latest version
          before retrying.
        </p>
      )}
      {applied && (
        <p role="status">
          {REVIEW_RESULTS[applied]}: {version.title}.
        </p>
      )}
    </section>
  )
}

export function CyclePage() {
  useTimestampPreference()
  const { cycleId = '' } = useParams()
  return <CycleWorkspace key={cycleId} cycleId={cycleId} />
}

function CycleWorkspace({ cycleId }: { cycleId: string }) {
  const enabled = useEnabled()
  const query = useCycle(cycleId, enabled)
  const consumer = useConsumer()
  const [params, setParams] = useSearchParams()
  const tab = steps.some(([key]) => key === params.get('tab'))
    ? params.get('tab')!
    : 'overview'
  const [saved, setSaved] = useState('')
  const [editing, setEditing] = useState<{
    kind: VersionWrite['kind']
    base?: Version
  } | null>(null)
  const selectedVersionId = params.get('version')
  useEffect(() => { setEditing(null) }, [tab, selectedVersionId])
  const executions = useExecutions(cycleId, enabled && Boolean(query.data) && tab === 'overview')
  const cycle = query.data
  const applications = useApplications(cycle?.project_id)
  const environments = useEnvironments(cycle?.app_id)
  if (!enabled) return <Disabled />
  if (!cycle)
    return query.error ? (
      query.error instanceof ApiError && [403, 404].includes(query.error.status) ? <StateCard kind={query.error.status === 403 ? 'unauthorized' : 'not-found'} title="Cycle unavailable" action={<Link className="btn btn-secondary" to="/cycles">Back to all cycles</Link>}>This cycle does not exist or is outside your authorized scope.</StateCard> : <ProblemCard
        title="Could not load cycle"
        message={query.error.message}
        onRetry={() => void query.refetch()}
      />
    ) : (
      <Skeleton label="Loading cycle" rows={2} />
    )
  const versions = cycle.versions.filter((v) => v.kind === tab)
  const version =
    versions.find((v) => v.id === params.get('version')) ?? versions.at(-1)
  const isDeliverable = [
    'scope',
    'test_plan',
    'readiness',
    'script',
    'report',
    'postmortem',
  ].includes(tab)
  const canEdit = canMutateAudience(consumer, cycle.project_id, cycle.app_id)
  const application =
    applications.data?.find((a) => a.id === cycle.app_id)?.name ?? cycle.app_id
  const environment =
    environments.data?.find((e) => e.id === cycle.environment_id)?.name ??
    cycle.environment_id
  return (
    <CycleActorNames cycleId={cycle.id} revision={cycle.revision}>
    <div className="cycle-workspace">
      <header className="cycle-heading">
        <div>
          <Link to="/cycles">All cycles</Link>
          <h2>{cycle.name}</h2>
          {cycle.origin_cycle_id && (
            <p>
              Continues{' '}
              <Link to={`/cycles/${encodeURIComponent(cycle.origin_cycle_id)}`}>
                the previous cycle
              </Link>
              . Reused inputs require fresh validation and review.
            </p>
          )}
          <p>
            <ProjectIdentity projectId={cycle.project_id} editable /> · {application} · {environment}
          </p>
          <p>
            Owner: <CycleActorIdentity id={cycle.owner} name={cycle.owner_name} /> ·{' '}
            <strong>
              {cycle.mode === 'simulation'
                ? 'Simulation — synthetic evidence'
                : 'Live providers'}
            </strong>
          </p>
        </div>
        <button
          className="btn btn-secondary"
          disabled={query.isFetching}
          onClick={() => void query.refetch()}
        >
          Refresh cycle
        </button>
      </header>
      <p className="cycle-next">
        Next action: {cycleNextAction(cycle)}
      </p>
      <nav className="cycle-nav" aria-label="Cycle workspace">
        {steps.map(([key, label]) => (
          <button
            key={key}
            className="btn btn-secondary"
            aria-current={tab === key ? 'page' : undefined}
            onClick={() => setParams({ tab: key })}
          >
            {label}
          </button>
        ))}
      </nav>
      {saved && <div><p role="status">Saved draft version.</p><CopyableId value={saved} label="Saved version ID" /></div>}
      {query.error && (
        <p role="alert">
          Refresh failed. Displaying the last loaded cycle:{' '}
          {query.error.message}
        </p>
      )}
      {tab === 'overview' && (
        <section className="cycle-panel glass-panel">
          <h2>Engineering deliverables</h2>
          <CycleProgress progress={cycle.progress} />
          <p>
            {cycle.independent_review
              ? 'Approval requires a distinct authenticated reviewer.'
              : 'An operator must explicitly approve each deliverable version.'}
          </p>
          <ul>
            {cycle.blockers.filter((b) => b !== cycleNextAction(cycle)).map((b) => (
              <li key={b}>{b}</li>
            ))}
          </ul>
          <div className="cycle-card-grid">
            {steps
              .filter(([key]) => key !== 'overview')
              .map(([key, label]) => {
                const current = cycle.versions
                  .filter((v) => v.kind === key)
                  .at(-1)
                return (
                  <div key={key}>
                    <h3>{label}</h3>
                    <p>
                      {key === 'executions' ? executions.data ? `${executions.data.preparations.length} prepared executions · ${executions.data.linked_runs.length} linked runs` : 'Open executions to review preparations and preflight checks' : current
                        ? `${current.title} · ${current.archived ? 'Archived' : current.status.replaceAll('_', ' ')}${current.status === 'approved' && !current.approval_current ? ' · Approval stale' : ''}`
                        : 'Not started'}
                    </p>
                    <button
                      className="btn btn-secondary"
                      onClick={() => setParams({ tab: key })}
                    >
                      Open {label}
                    </button>
                  </div>
                )
              })}
          </div>
        </section>
      )}
      {tab === 'executions' && (
        <ExecutionWorkspace cycle={cycle} canEdit={canEdit} />
      )}
      {isDeliverable && !editing && (
        <>
          <div className="cycle-heading">
            <h2>{steps.find(([key]) => key === tab)?.[1]}</h2>
            {canEdit && (
              <div className="cycle-actions">
                <button
                  className="btn btn-primary"
                  onClick={() =>
                    setEditing({
                      kind: tab as VersionWrite['kind'],
                      base: version,
                    })
                  }
                >
                  {version ? 'Revise as new draft' : 'Create draft'}
                </button>
                {tab === 'script' && version && (
                  <button
                    className="btn btn-secondary"
                    onClick={() => setEditing({ kind: 'script' })}
                  >
                    Import another script
                  </button>
                )}
              </div>
            )}
          </div>
          {tab === 'report' && <CycleResults cycleId={cycle.id} projectId={cycle.project_id} />}
          {tab === 'postmortem' && (
            <CycleHandoff cycle={cycle} canEdit={canEdit} />
          )}
          {tab === 'script' && (
            <ProviderScriptImport cycle={cycle} canEdit={canEdit} />
          )}
          {versions.length > 0 ? (
            <>
              <label className="cycle-field">
                Selected version
                <select
                  value={version?.id}
                  onChange={(e) => setParams({ tab, version: e.target.value })}
                >
                  {versions.map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.title} · {v.id} ·{' '}
                      {v.archived ? 'archived' : v.status}
                    </option>
                  ))}
                </select>
              </label>
              {tab === 'script' && version && !editing && (
                <ScriptStorage
                  key={`storage-${version.id}`}
                  cycle={cycle}
                  version={version}
                  canEdit={canEdit}
                />
              )}
              {tab === 'scope' && version && (
                <CycleSources
                  key={`sources-${version.id}`}
                  cycle={cycle}
                  version={version}
                  canEdit={canEdit}
                />
              )}
              {tab === 'report' && version && (
                <ReportEvidence
                  key={`evidence-${version.id}`}
                  cycle={cycle}
                  version={version}
                  canEdit={canEdit}
                />
              )}
              {version && (
                <VersionDetails
                  key={`details-${version.id}`}
                  cycle={cycle}
                  version={version}
                  onReviewed={() => setSaved('')}
                />
              )}
            </>
          ) : (
            <StateCard kind="empty" compact title="No deliverable version yet." action={null}>Create a draft using the controls above.</StateCard>
          )}
        </>
      )}
      {editing && (
        <CycleEditor
          key={`${cycle.id}-${editing.kind}-${editing.base?.id ?? 'new'}`}
          cycle={cycle}
          {...editing}
          onCancel={() => setEditing(null)}
          onSaved={(id) => {
            setSaved(id)
            setEditing(null)
            setParams({ tab: editing.kind, version: id })
          }}
        />
      )}
    </div>
    </CycleActorNames>
  )
}
