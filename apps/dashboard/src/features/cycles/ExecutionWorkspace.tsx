import { useRef, useState } from 'react'
import { useEnvironments } from '@/api/hooks/useCatalog'
import { useOptionalConsumer } from '@/auth/AuthProvider'
import { formatTimestamp } from '@/utils/time'
import { useRunNames } from './useRunNames'
import { CopyableId } from '@/components/ux/CopyableId'
import { actorLabel } from './cycleDisplay'
import { ProviderValidation } from './ProviderValidation'
import { Link } from 'react-router'
import {
  useExecutions,
  useLinkRun,
  usePrepareExecution,
  type Cycle,
  type Preparation,
} from './api'

export function ExecutionWorkspace({
  cycle,
  canEdit,
}: {
  cycle: Cycle
  canEdit: boolean
}) {
  const names = useRunNames(cycle.project_id)
  const consumer = useOptionalConsumer()
  const environments = useEnvironments(cycle.app_id)
  const environmentName = environments.data?.find(item => item.id === cycle.environment_id)?.name ?? cycle.environment_id
  const query = useExecutions(cycle.id)
  const prepare = usePrepareExecution(cycle.id)
  const link = useLinkRun(cycle.id)
  const [planId, setPlanId] = useState('')
  const [scriptId, setScriptId] = useState('')
  const [readinessId, setReadinessId] = useState('')
  const [connectionId, setConnectionId] = useState('')
  const [authority, setAuthority] = useState<'pipeline' | 'native_schedule'>(
    'pipeline',
  )
  const [runId, setRunId] = useState('')
  const request = useRef<{ signature: string; key: string } | null>(null)
  const plans = cycle.versions.filter(
    (v) => v.kind === 'test_plan' && v.approval_current,
  )
  const scripts = cycle.versions.filter(
    (v) =>
      v.kind === 'script' &&
      v.approval_current &&
      v.dependencies.includes(planId),
  )
  const readiness = cycle.versions.filter(
    (v) =>
      v.kind === 'readiness' &&
      v.approval_current &&
      v.dependencies.includes(planId),
  )
  const plan = plans.find((v) => v.id === planId)
  const script = scripts.find((v) => v.id === scriptId)
  const data = query.data
  const busy = prepare.isPending || link.isPending
  const matchingConnections = data?.connections.filter(connection => connection.provider === String(script?.content.engine).replaceAll('-', '_')) ?? []
  return (
    <section className="cycle-panel glass-panel">
      <h2>Executions</h2>
      <p>
        Freeze an approved script and workload for preflight review. Cycle
        dispatch is not enabled; preparing an execution does not launch a test.
      </p>
      <p>
        <strong>
          {cycle.mode === 'simulation'
            ? 'Simulation — no live provider'
            : 'Live cycle'}
        </strong>{' '}
        · Executions sharing this environment must run sequentially.
      </p>
      {query.isPending && <p role="status">Loading execution prerequisites…</p>}
      {query.error && (
        <p role="alert">
          {query.error.message}{' '}
          <button className="btn btn-secondary" onClick={() => void query.refetch()}>Retry</button>
        </p>
      )}
      {data && (
        <>
          <button
            className="btn btn-secondary"
            disabled={query.isFetching || busy}
            onClick={() => void query.refetch()}
          >
            Recheck prerequisites
          </button>
          <p>
            Readiness references: inventory{' '}
            <code>{data.inventory_snapshot_id ?? 'Not available'}</code>, target
            version {data.target_version ?? 'Not available'}.{' '}
            {data.inventory_scanned_at &&
              `Scanned ${formatTimestamp(data.inventory_scanned_at)}.`}
          </p>
          {canEdit && (
            <form
              className="cycle-form"
              onSubmit={(event) => {
                event.preventDefault()
                if (!plan || !script) return
                const logical: Omit<
                  Preparation,
                  'idempotency_key' | 'expected_revision'
                > = {
                  plan_version_id: plan.id,
                  script_version_id: script.id,
                  readiness_version_id: readinessId || null,
                  connection_id: connectionId || null,
                  launch_authority: authority,
                }
                const signature = JSON.stringify(logical)
                if (request.current?.signature !== signature)
                  request.current = {
                    signature,
                    key: crypto.randomUUID().replaceAll('-', ''),
                  }
                prepare.mutate({
                  ...logical,
                  expected_revision: data.revision,
                  idempotency_key: request.current.key,
                })
              }}
            >
              <fieldset disabled={busy} className="cycle-execution-fields">
                <legend>Prepare an execution</legend>
                <label className="cycle-field">
                  Approved test plan
                  <select
                    required
                    value={planId}
                    onChange={(event) => {
                      setPlanId(event.target.value)
                      setScriptId('')
                      setReadinessId('')
                      setConnectionId('')
                      setAuthority('pipeline')
                      prepare.reset()
                    }}
                  >
                    <option value="">Select a plan</option>
                    {plans.map((v) => (
                      <option key={v.id} value={v.id}>
                        {v.title} · {v.id}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="cycle-field">
                  Approved script
                  <select
                    required
                    value={scriptId}
                    onChange={(event) => {
                      setScriptId(event.target.value)
                      setConnectionId('')
                      setAuthority('pipeline')
                      prepare.reset()
                    }}
                  >
                    <option value="">Select a script for this plan</option>
                    {scripts.map((v) => (
                      <option key={v.id} value={v.id}>
                        {v.title} · {String(v.content.engine)} ·{' '}
                        {v.id}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="cycle-field">
                  Approved readiness
                  <select
                    value={readinessId}
                    onChange={(event) => {
                      setReadinessId(event.target.value)
                      prepare.reset()
                    }}
                  >
                    <option value="">
                      Not selected — preflight will be blocked
                    </option>
                    {readiness.map((v) => (
                      <option key={v.id} value={v.id}>
                        {v.title} · {v.id}
                      </option>
                    ))}
                  </select>
                </label>
                {cycle.mode === 'live' && (
                  <label className="cycle-field">
                    Execution connection
                    <select
                      value={connectionId}
                      onChange={(event) => {
                        setConnectionId(event.target.value)
                        prepare.reset()
                      }}
                    >
                      <option value="">
                        Not selected — preflight will be blocked
                      </option>
                      {matchingConnections.map((c) => (
                          <option key={c.id} value={c.id}>
                            {c.name} · {c.provider}
                          </option>
                        ))}
                    </select>
                  </label>
                )}
                {cycle.mode === 'live' && script && matchingConnections.length === 0 && <p className="tonal-card warning">No authorized connection matches this script’s engine. Ask an administrator to configure a connection for this project and application before preflight can pass.</p>}
                {script?.content.engine === 'apex-load' && <label className="cycle-field">
                  Launch owner
                  <select
                    value={authority}
                    onChange={(event) => {
                      setAuthority(event.target.value as typeof authority)
                      prepare.reset()
                    }}
                  >
                    <option value="pipeline">APEX pipeline</option>
                    {script?.content.engine === 'apex-load' && (
                      <option value="native_schedule">
                        Native APEX Load schedule
                      </option>
                    )}
                  </select>
                </label>}
                {plan && (
                  <p>
                    Workload: {String(plan.content.vusers)} VUsers ·{' '}
                    {String(plan.content.ramp_s)}s ramp ·{' '}
                    {String(plan.content.duration_s)}s duration. Target:{' '}
                    <span>{environmentName}</span>.
                  </p>
                )}
                {script && (
                  <p>
                    Script: {script.title} · {String(script.content.format)}.
                    Version <code>{script.id}</code> · checksum{' '}
                    <code>{script.checksum}</code>.
                  </p>
                )}
                <button
                  className="btn btn-primary"
                  disabled={!plan || !script}
                  type="submit"
                >
                  {prepare.isPending ? 'Preparing…' : 'Prepare execution'}
                </button>
              </fieldset>
            </form>
          )}
          {prepare.error && <p role="alert">{prepare.error.message}</p>}
          {prepare.isSuccess && (
            <p role="status">
              Execution preparation saved. Review its blockers below.
            </p>
          )}
          <h3>Prepared executions</h3>
          {data.preparations.length === 0 && (
            <p>
              No execution preparations yet. Approve a test plan and a script
              that references it to begin.
            </p>
          )}
          {data.preparations.map((p) => (
            <article className="cycle-panel" key={p.id}>
              <h4>
                {p.snapshot.script_title} · {p.snapshot.engine}
              </h4>
              <p>
                <strong>Launch blocked</strong> · {p.snapshot.mode} ·{' '}
                {p.snapshot.launch_authority === 'native_schedule'
                  ? 'Native schedule owns launch'
                  : 'Pipeline owns launch'}{' '}
                · sequential execution
              </p>
              <p>
                Plan:{' '}
                <Link
                  to={`?tab=test_plan&version=${encodeURIComponent(p.snapshot.plan_version_id)}`}
                >
                  {p.snapshot.plan_title}
                </Link>
                . Script:{' '}
                <Link
                  to={`?tab=script&version=${encodeURIComponent(p.snapshot.script_version_id)}`}
                >
                  {p.snapshot.script_title}
                </Link>
                .
              </p>
              <p>
                {String(p.snapshot.workload.vusers)} VUsers ·{' '}
                {String(p.snapshot.workload.ramp_s)}s ramp ·{' '}
                {String(p.snapshot.workload.duration_s)}s duration
              </p>
              {(['blocked', 'not_checked', 'ready'] as const).map(status => {
                const checks = p.checks.filter(check => check.status === status)
                return checks.length > 0 && <section key={status} aria-label={`${status.replaceAll('_', ' ')} checks`}>
                  <h5><span className={`status-badge ${status === 'blocked' ? 'danger' : status === 'ready' ? 'success' : 'neutral'}`}>{status === 'blocked' ? 'Blocked' : status === 'ready' ? 'Ready' : 'Not checked'} · {checks.length}</span></h5>
                  <ul>{checks.map(check => <li key={check.key}><strong>{check.label}</strong> — {check.detail}</li>)}</ul>
                </section>
              })}
              <ProviderValidation
                cycleId={cycle.id}
                preparationId={p.id}
                canEdit={canEdit}
              />
              <details>
                <summary>Binding and audit details</summary>
                <dl>
                  <dt>Preparation</dt>
                  <dd><CopyableId value={p.id} label="Preparation ID" /></dd>
                  <dt>Prepared by</dt>
                  <dd>{actorLabel(p.created_by, consumer)}</dd>
                  <dt>Prepared at</dt>
                  <dd>{formatTimestamp(p.created_at)}</dd>
                  <dt>Preflight checked</dt>
                  <dd>{formatTimestamp(p.checked_at)}</dd>
                  <dt>Script source SHA-256</dt>
                  <dd>
                    <code>{p.snapshot.script_source_checksum}</code>
                  </dd>
                  <dt>Environment / target version</dt>
                  <dd>
                    {p.snapshot.environment_id} /{' '}
                    {p.snapshot.environment_version ?? 'Unavailable'}
                  </dd>
                  <dt>Connection</dt>
                  <dd>{p.snapshot.connection_id ?? 'Not selected'}</dd>
                </dl>
              </details>
            </article>
          ))}
          <h3>Related existing runs</h3>
          <p>
            Links preserve existing run history. Their approved script and
            workload bindings are not verified.
          </p>
          {canEdit && (
            <form
              className="cycle-form"
              onSubmit={(event) => {
                event.preventDefault()
                link.mutate({
                  expected_revision: data.revision,
                  engine_run_id: runId,
                })
              }}
            >
              <label className="cycle-field">
                Recent run in this project and application
                <select
                  required
                  disabled={busy}
                  value={runId}
                  onChange={(event) => setRunId(event.target.value)}
                >
                  <option value="">Select an existing run</option>
                  {data.recent_runs
                    .filter((r) => !data.linked_runs.some((l) => l.id === r.id))
                    .map((r) => (
                      <option key={r.id} value={r.id}>
                        {names.get(r.thread_id) || 'Title unavailable'} · {r.engine} attempt {r.attempt} · {r.status} ·{' '}
                        {formatTimestamp(r.started_at)} ·{' '}
                        {r.thread_id}
                      </option>
                    ))}
                </select>
              </label>
              <button className="btn btn-secondary" disabled={busy || !runId}>
                Link existing run
              </button>
            </form>
          )}
          <p>The selector shows up to 50 recent scoped runs.</p>
          {link.error && <p role="alert">{link.error.message}</p>}
          {link.isSuccess && (
            <p role="status">
              Existing run linked. Bindings remain unverified.
            </p>
          )}
          <ul>
            {data.linked_runs.map((r) => (
              <li key={r.id}>
                <Link
                  to={`/runs/${encodeURIComponent(r.thread_id)}/phases/execution`}
                >
                  {names.get(r.thread_id) || 'Title unavailable'} · {r.engine} attempt {r.attempt} · {formatTimestamp(r.started_at)}
                </Link>{' '}<CopyableId value={r.thread_id} label="Run ID" />{' '}
                · engine status {r.status} · bindings unverified
              </li>
            ))}
          </ul>
          <Link to="/runs">Inspect test history</Link>
        </>
      )}
    </section>
  )
}
