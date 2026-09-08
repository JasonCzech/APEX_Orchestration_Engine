import { CycleSources } from './CycleSources'
import { CycleHandoff } from './CycleHandoff'
import { FrozenEvidenceDiff } from './FrozenEvidenceDiff'
import { ReportEvidence } from './ReportEvidence'
import { CycleResults } from './CycleResults'
import { useState, type FormEvent } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'
import { useAuth, useConsumer } from '@/auth/AuthProvider'
import { roleAtLeast } from '@/auth/RequireRole'
import { useApplications, useEnvironments } from '@/api/hooks/useCatalog'
import { ProblemCard } from '@/components/ProblemCard'
import { ProviderScriptImport } from './ProviderScriptImport'
import { ScriptStorage } from './ScriptStorage'
import { ExecutionWorkspace } from './ExecutionWorkspace'
import { CycleEditor } from './CycleEditor'
import {
  useCycle,
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
  ['test_plan', 'Test Plan'],
  ['readiness', 'Readiness'],
  ['script', 'Scripts'],
  ['executions', 'Executions'],
  ['report', 'Results & Reports'],
  ['postmortem', 'Postmortem'],
] as const
const actions: Record<string, string> = {
  submit: 'Submit for review',
  approve: 'Approve this version',
  request_revision: 'Request revision',
  archive: 'Archive in APEX',
  restore: 'Restore in APEX',
}
function useEnabled() {
  const { state } = useAuth()
  return (
    state.status === 'authenticated' &&
    state.systemInfo.features.cycles === true
  )
}
function Disabled() {
  return (
    <section className="glass-panel cycle-panel">
      <h1>Test Cycles</h1>
      <p>The cycle workspace is not enabled on this server.</p>
      <Link to="/runs">Open Test History</Link>
    </section>
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
    if (!create.isPending)
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
        <label className="cycle-field">
          Project
          <input
            required
            value={project}
            onChange={(e) => {
              setProject(e.target.value)
              setApp('')
              setEnvironment('')
            }}
          />
        </label>
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
          disabled={applications.isFetching || environments.isFetching}
          type="submit"
        >
          {create.isPending ? 'Creating…' : 'Create cycle'}
        </button>
      </fieldset>
      {(create.error || applications.error || environments.error) && (
        <p role="alert">
          {(create.error || applications.error || environments.error)?.message}
        </p>
      )}
    </form>
  )
}

export function CyclesPage() {
  const enabled = useEnabled()
  const consumer = useConsumer()
  const [offset, setOffset] = useState(0)
  const [newOpen, setNewOpen] = useState(false)
  const [created, setCreated] = useState('')
  const query = useCycles(offset, enabled)
  if (!enabled) return <Disabled />
  return (
    <div className="cycle-workspace">
      <header className="cycle-heading">
        <div>
          <h1>Test Cycles</h1>
          <p>
            Plan, review and maintain engineering deliverables across test
            attempts.
          </p>
        </div>
        {roleAtLeast(consumer?.role ?? 'viewer', 'operator') && (
          <button
            className="btn btn-primary"
            onClick={() => setNewOpen(!newOpen)}
          >
            {newOpen ? 'Close new cycle' : 'New cycle'}
          </button>
        )}
      </header>
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
      {query.isPending && <p role="status">Loading cycles…</p>}
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
              {cycle.project_id} ·{' '}
              {cycle.mode === 'simulation' ? 'Simulation' : 'Live providers'}
            </p>
            <p>Owner: {cycle.owner}</p>
            <small>
              Updated {new Date(cycle.updated_at).toLocaleString()} · revision{' '}
              {cycle.revision}
            </small>
          </article>
        ))}
      </div>
      {query.data?.length === 0 && (
        <p>
          No cycles on this page. Create a cycle to assemble your first
          deliverables.
        </p>
      )}
      <div className="cycle-actions">
        <button
          className="btn btn-secondary"
          disabled={offset === 0 || query.isFetching}
          onClick={() => setOffset(Math.max(0, offset - 20))}
        >
          Previous
        </button>
        <span>Page {offset / 20 + 1}</span>
        <button
          className="btn btn-secondary"
          disabled={query.data?.length !== 20 || query.isFetching}
          onClick={() => setOffset(offset + 20)}
        >
          Next
        </button>
      </div>
    </div>
  )
}

function VersionDetails({
  cycle,
  version,
}: {
  cycle: Cycle
  version: Version
}) {
  const review = useReview(cycle.id)
  const [note, setNote] = useState('')
  const [compare, setCompare] = useState('')
  const [reportView, setReportView] = useState('technical')
  const baseline = cycle.versions.find((v) => v.id === compare)
  const fields = cycle.fields?.[version.kind] ?? []
  const reviewed = version.status === 'approved'
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
        version {version.id.slice(0, 8)}
      </p>
      <p>{version.change_summary}</p>
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
        Created by {version.created_by} ·{' '}
        {new Date(version.created_at).toLocaleString()}
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
                {v.title} · {v.id.slice(0, 8)} · {v.status}
              </option>
            ))}
        </select>
      </label>
      {version.kind === 'report' && baseline && (
        <FrozenEvidenceDiff
          selected={version.content._execution_evidence}
          baseline={baseline.content._execution_evidence}
        />
      )}
      <div className={baseline ? 'cycle-diff' : ''}>
        {fields
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
              {baseline && (
                <div>
                  <strong>Comparison version</strong>
                  <p>{String(baseline.content[field.key] ?? 'Not provided')}</p>
                </div>
              )}
              <div>
                {baseline && <strong>Selected version</strong>}
                <p>{String(version.content[field.key] ?? 'Not provided')}</p>
              </div>
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
              {upstream?.title ?? 'Missing upstream version'} · {id.slice(0, 8)}{' '}
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
        <p>Version ID: {version.id}</p>
        <p>Content SHA-256: {version.checksum}</p>
        <ul>
          {version.reviews.map((r, i) => (
            <li key={i}>
              {r.action} · {r.actor} · {r.at}
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
            {version.available_actions.map((action) => (
              <button
                type="button"
                className={
                  action === 'approve' ? 'btn btn-primary' : 'btn btn-secondary'
                }
                disabled={review.isPending}
                key={action}
                onClick={() =>
                  review.mutate({
                    action: action as Review['action'],
                    version_id: version.id,
                    expected_revision: cycle.revision,
                    note,
                  })
                }
              >
                {actions[action] ?? action}
              </button>
            ))}
          </div>
        </>
      )}
      {review.error && (
        <p role="alert">
          {review.error.message} Refresh the cycle and review the latest version
          before retrying.
        </p>
      )}
      {review.isSuccess && (
        <p role="status">
          Review action recorded for version {version.id.slice(0, 8)}.
        </p>
      )}
    </section>
  )
}

export function CyclePage() {
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
  const cycle = query.data
  const applications = useApplications(cycle?.project_id)
  const environments = useEnvironments(cycle?.app_id)
  if (!enabled) return <Disabled />
  if (!cycle)
    return query.error ? (
      <ProblemCard
        title="Could not load cycle"
        message={query.error.message}
        onRetry={() => void query.refetch()}
      />
    ) : (
      <p role="status">Loading cycle…</p>
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
  const canEdit = roleAtLeast(consumer?.role ?? 'viewer', 'operator')
  const application =
    applications.data?.find((a) => a.id === cycle.app_id)?.name ?? cycle.app_id
  const environment =
    environments.data?.find((e) => e.id === cycle.environment_id)?.name ??
    cycle.environment_id
  return (
    <div className="cycle-workspace">
      <header className="cycle-heading">
        <div>
          <Link to="/cycles">All cycles</Link>
          <h1>{cycle.name}</h1>
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
            {cycle.project_id} · {application} · {environment}
          </p>
          <p>
            Owner: {cycle.owner} ·{' '}
            <strong>
              {cycle.mode === 'simulation'
                ? 'Simulation — synthetic evidence'
                : 'Live providers'}
            </strong>
          </p>
        </div>
        <button
          className="btn btn-secondary"
          disabled={query.isFetching || Boolean(editing)}
          onClick={() => void query.refetch()}
        >
          Refresh cycle
        </button>
      </header>
      <p className="cycle-next">
        Next action: {cycle.blockers[0] ?? 'Review deliverables'}
      </p>
      <nav className="cycle-nav" aria-label="Cycle workspace">
        {steps.map(([key, label]) => (
          <button
            key={key}
            className="btn btn-secondary"
            aria-current={tab === key ? 'page' : undefined}
            disabled={Boolean(editing)}
            onClick={() => setParams({ tab: key })}
          >
            {label}
          </button>
        ))}
      </nav>
      {saved && <p role="status">Saved draft version {saved.slice(0, 8)}.</p>}
      {query.error && (
        <p role="alert">
          Refresh failed. Displaying the last loaded cycle:{' '}
          {query.error.message}
        </p>
      )}
      {tab === 'overview' && (
        <section className="cycle-panel glass-panel">
          <h2>Engineering deliverables</h2>
          <p>
            {cycle.independent_review
              ? 'Approval requires a distinct authenticated reviewer.'
              : 'An operator must explicitly approve each deliverable version.'}
          </p>
          <ul>
            {cycle.blockers.map((b) => (
              <li key={b}>{b}</li>
            ))}
          </ul>
          <div className="cycle-card-grid">
            {steps
              .filter(([key]) => !['overview', 'executions'].includes(key))
              .map(([key, label]) => {
                const current = cycle.versions
                  .filter((v) => v.kind === key)
                  .at(-1)
                return (
                  <div key={key}>
                    <h3>{label}</h3>
                    <p>
                      {current
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
          {tab === 'report' && <CycleResults cycleId={cycle.id} />}
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
                      {v.title} · {v.id.slice(0, 8)} ·{' '}
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
                />
              )}
            </>
          ) : (
            <p>No deliverable version yet.</p>
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
  )
}
