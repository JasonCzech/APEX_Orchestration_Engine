import { useTimestampPreference } from '@/theme/preferences'
import { StateCard } from '@/components/ux/StateCard'
import { Skeleton } from '@/components/ux/Skeleton'
import { useProjectLabels } from '@/api/hooks/useProjectLabels'
import { usePipelines } from '@/api/hooks/usePipelines'
import { usePromptList } from '@/api/hooks/usePrompts'
import { useTopbarTitle } from '@/components/layout/TopbarContributionProvider'
/**
 * /environments/:id — reference card + live k8s inventory (plan Part 2 route
 * table, D5).
 *
 * Two panels: (1) the editable reference (base_url / kind / hosts / options —
 * PATCH, operator+), (2) the inventory snapshot with the synchronous Rescan
 * action. Rescan failures are 502 problems whose detail carries the adapter
 * message — they render as an inline danger card with a retry (probe-style;
 * deliberately NOT a toast).
 */
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'

import {
  environmentWriteMutationKey,
  useApplicationsIndex,
  useEnvironment,
  useEnvironmentsIndex,
  useUpdateEnvironment,
  type Environment,
  type EnvironmentUpdate,
} from '@/api/hooks/useEnvironments'
import { usePendingMutationCount } from '@/api/hooks/usePendingMutationCount'
import {
  useEnvironmentInventory,
  useRescanEnvironment,
  type SnapshotView,
} from '@/api/hooks/useInventory'
import { useConsumer } from '@/auth/AuthProvider'
import { canMutateAudience, isGlobalAdmin } from '@/auth/RequireRole'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { ProblemCard } from '@/components/ProblemCard'
import { JsonViewer } from '@/components/viewers/JsonViewer'
import { formatRelative } from '@/utils/time'

import { HostsEditor } from './environmentsForm'
import { EnvironmentTarget } from './EnvironmentTarget'
import { DeleteEnvironmentDialog } from './environmentsDialogs'
import { TargetBadge } from './EnvironmentsPage'
import {
  KIND_LABELS,
  KIND_OPTIONS,
  kindLabel,
  hostsToDrafts,
  hostsToPayload,
  parseOptionsJson,
  type HostDraft,
} from './environmentsLogic'
import './environments.css'

const EM_DASH = '—'

/** Read-mode reference card: base_url, hosts table, options JSON. */
function ReferenceCard({ environment }: { environment: Environment }) {
  return (
    <div className="env-card glass-panel">
      <h2 className="env-card-title">Configuration</h2>
      <dl className="env-ref-grid">
        <dt>Platform</dt>
        <dd>{kindLabel(environment.kind) ?? <span className="env-muted">{EM_DASH}</span>}</dd>
        <dt>Target URL</dt>
        <dd>
          {environment.base_url ? (
            <span className="env-base-url" title={environment.base_url}>
              {environment.base_url}
            </span>
          ) : (
            <span className="env-muted">{EM_DASH}</span>
          )}
        </dd>
      </dl>
      <h3 className="env-card-subtitle">Hosts</h3>
      {environment.hosts.length === 0 ? (
        <p className="env-muted">No hosts recorded.</p>
      ) : (
        <div className="data-table-wrap">
          <table className="data-table env-hosts-table" aria-label="Hosts">
            <thead>
              <tr>
                <th>Hostname</th>
                <th>Role</th>
              </tr>
            </thead>
            <tbody>
              {environment.hosts.map((host) => (
                <tr key={host.id}>
                  <td className="env-base-url">{host.hostname}</td>
                  <td>{host.role ?? EM_DASH}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <details className="env-advanced-options">
        <summary>Advanced options</summary>
        <JsonViewer value={environment.options} ariaLabel="Environment options" />
      </details>
    </div>
  )
}

/** Inline edit form for base_url / kind / hosts / options (PATCH; operator+). */
function EditEnvironmentForm({
  environment,
  siblingNames,
  canApproveTarget,
  onDone,
}: {
  environment: Environment
  siblingNames: string[]
  canApproveTarget: boolean
  onDone: (environmentId: string) => void
}) {
  const update = useUpdateEnvironment(environment.id)
  const writeCount = usePendingMutationCount(environmentWriteMutationKey(environment.id))
  const [name, setName] = useState(environment.name)
  const [baseUrl, setBaseUrl] = useState(environment.base_url ?? '')
  const [kind, setKind] = useState(environment.kind ?? '')
  const [hosts, setHosts] = useState<HostDraft[]>(() => hostsToDrafts(environment.hosts))
  const [optionsText, setOptionsText] = useState(() =>
    JSON.stringify(environment.options, null, 2),
  )

  const optionsParse = parseOptionsJson(optionsText)
  const optionsChanged = optionsText !== JSON.stringify(environment.options, null, 2)
  const hasReservedOptions =
    optionsParse.ok && [...Object.keys(optionsParse.value), ...Object.keys(environment.options)]
      .some((key) => key.startsWith('_apex_'))
  const duplicate = name.trim() !== environment.name && siblingNames.includes(name.trim())
  const canSave =
    name.trim() !== '' && !duplicate &&
    optionsParse.ok && (canApproveTarget || !optionsChanged || !hasReservedOptions) && writeCount === 0

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSave || !optionsParse.ok) return
    update.mutate(
      {
        environmentId: environment.id,
        body: (() => {
          const body: EnvironmentUpdate = {}
          if (name.trim() !== environment.name) body.name = name.trim()
          if ((baseUrl.trim() || null) !== (environment.base_url ?? null)) body.base_url = baseUrl.trim() || null
          if ((kind || null) !== environment.kind) body.kind = kind || null
          const nextHosts = hostsToPayload(hosts)
          const currentHosts = (environment.hosts ?? []).map(({ hostname, role }) => ({ hostname, role }))
          if (JSON.stringify(nextHosts) !== JSON.stringify(currentHosts)) body.hosts = nextHosts
          if (optionsText !== JSON.stringify(environment.options, null, 2)) body.options = optionsParse.value
          return body
        })(),
      },
      { onSuccess: () => onDone(environment.id) },
    )
  }

  return (
    <form className="env-card glass-panel" onSubmit={submit} aria-label="Edit environment">
      <h2 className="env-card-title">Edit environment</h2>
      <label className="env-field">
        <span className="env-field-label">Name</span>
        <input type="text" className="field-input" aria-label="Name" value={name} onChange={(event) => setName(event.target.value)} aria-invalid={duplicate || undefined} />
      </label>
      {duplicate && <p className="env-form-error" role="alert">This application already has an environment with this name.</p>}
      <div className="env-form-grid">
        {canApproveTarget && <label className="env-field">
          <span className="env-field-label">Execution target URL</span>
          <small>Saving a changed target URL validates and approves it as a new target.</small>
          <input
            type="text"
            className="field-input"
            aria-label="Execution target URL"
            value={baseUrl}
            onChange={(event) => setBaseUrl(event.target.value)}
          />
        </label>}
        <label className="env-field">
          <span className="env-field-label">Platform</span>
          <select
            className="field-select"
            aria-label="Platform"
            value={kind}
            onChange={(event) => setKind(event.target.value)}
          >
            <option value="">— unspecified —</option>
            {/* Preserve a non-standard kind already on the record. */}
            {!KIND_OPTIONS.includes(kind as (typeof KIND_OPTIONS)[number]) && (
              <option value={kind}>{kind}</option>
            )}
            {KIND_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {KIND_LABELS[option]}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="env-field">
        <span className="env-field-label">Hosts</span>
        <HostsEditor hosts={hosts} onChange={setHosts} />
      </div>
      <details className="env-advanced-options" open={optionsChanged || undefined}>
        <summary>Advanced options</summary>
        <label className="env-field">
          <span className="env-field-label">Options (JSON)</span>
          <textarea
            className="field-input env-options-input"
            aria-label="Options JSON"
            rows={6}
            spellCheck={false}
            value={optionsText}
            onChange={(event) => setOptionsText(event.target.value)}
          />
          <span className="env-field-help">Connection-specific settings, as a JSON object. Keys starting with <code>_apex_</code> are reserved for global administrators.</span>
        </label>
      </details>
      {!optionsParse.ok && (
        <p className="env-form-error" role="alert">
          {optionsParse.message}
        </p>
      )}
      {!canApproveTarget && optionsChanged && hasReservedOptions && (
        <p className="env-form-error" role="alert">
          Options beginning with _apex_ require a global administrator.
        </p>
      )}
      {update.isError && (
        <div className="env-inline-error" role="alert">
          <span>Save failed: {update.error.message}</span>
        </div>
      )}
      <div className="env-panel-actions">
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => onDone(environment.id)}
          disabled={update.isPending}
        >
          Cancel
        </button>
        <button type="submit" className="btn btn-primary btn-sm" disabled={!canSave}>
          {update.isPending ? 'Saving…' : 'Save changes'}
        </button>
      </div>
    </form>
  )
}

function ServicesTable({ snapshot }: { snapshot: SnapshotView }) {
  return (
    <div className="data-table-wrap">
      <table className="data-table striped env-services-table" aria-label="Services">
        <thead>
          <tr>
            <th>Service</th>
            <th className="num">Replicas</th>
            <th>Image</th>
          </tr>
        </thead>
        <tbody>
          {snapshot.services.map((service) => (
            <tr key={service.name} data-testid={`env-service-${service.name}`}>
              <td className="strong">{service.name}</td>
              <td className="num">
                {service.replicas === 0 ? (
                  <span className="status-badge danger">0</span>
                ) : (
                  service.replicas
                )}
              </td>
              <td>
                <span className="env-image" title={service.image || undefined}>
                  {service.image || EM_DASH}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Inventory panel: never-scanned empty state, services table, rescan errors. */
function InventoryPanel({
  environmentId,
  canMutate,
  rescan,
  writePending,
}: {
  environmentId: string
  canMutate: boolean
  rescan: ReturnType<typeof useRescanEnvironment>
  writePending: boolean
}) {
  const inventory = useEnvironmentInventory(environmentId)

  return (
    <div className="env-card glass-panel">
      <header className="env-card-header">
        <h2 className="env-card-title">Inventory</h2>
        {canMutate && inventory.data?.snapshot && <button type="button" className="btn btn-secondary btn-sm env-scan-button" onClick={() => rescan.mutate()} disabled={writePending}>{rescan.isPending ? 'Scanning…' : 'Rescan'}</button>}
        {inventory.data?.snapshot && (
          <span className="env-scan-caption">
            Scanned {formatRelative(inventory.data.snapshot.scanned_at)} · {inventory.data.snapshot.source ?? 'Scan source not recorded'}{inventory.data.snapshot.connection_name ? ` · ${inventory.data.snapshot.connection_name}` : ''}
            {inventory.data.snapshot.stale && (
              <span className="status-badge warning env-stale-chip">stale</span>
            )}
          </span>
        )}
      </header>

      {inventory.data?.snapshot?.source === 'Simulated inventory' && <p className="tonal-card warning">This is a fixed demonstration inventory. It does not describe the environment’s live hosts.</p>}
      {rescan.isError && (
        <div className="env-inline-error" role="alert">
          <span>{rescan.error.message}</span>
          {canMutate && (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => rescan.mutate()}
              disabled={writePending}
            >
              Retry
            </button>
          )}
        </div>
      )}

      {inventory.isError && inventory.data && (
        <CachedDataWarning error={inventory.error} onRetry={() => void inventory.refetch()} />
      )}

      {inventory.isPending ? (
        <Skeleton label="Loading inventory" rows={2} />
      ) : inventory.isError && !inventory.data ? (
        <div className="env-inline-error" role="alert">
          <span>{inventory.error.message}</span>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => void inventory.refetch()}
          >
            Retry
          </button>
        </div>
      ) : !inventory.data.snapshot ? (
        <StateCard kind="unconfigured" title="Never scanned" compact data-testid="env-inventory-empty"
          action={canMutate ? <button
              type="button"
              className="btn btn-secondary"
              onClick={() => rescan.mutate()}
              disabled={writePending}
            >
              {rescan.isPending ? 'Scanning…' : 'Scan'}
            </button> : null}>
          <p>{canMutate ? 'Run a scan to capture the services deployed in this environment.' : 'Ask a project operator to scan the services deployed in this environment.'}</p>
        </StateCard>
      ) : (
        <ServicesTable snapshot={inventory.data.snapshot} />
      )}
    </div>
  )
}

/** Environment-context prompts and recent tests that used this environment. */
function RelatedCard({ environment, projectId, projectPending, canAddContext }: {
  environment: Environment
  projectId: string | undefined
  projectPending: boolean
  canAddContext: boolean
}) {
  const prompts = usePromptList({ namespace: 'environment', environment_id: environment.id })
  // The newest tests in the project, narrowed to those whose scope includes this environment.
  const recent = usePipelines({ project: projectId, limit: 100 })
  const tests = (recent.data?.items ?? []).filter(test => {
    const participants = (test.test_scope as { participants?: Array<{ environment_id?: string }> } | null | undefined)?.participants
    return participants?.some(participant => participant.environment_id === environment.id)
  }).slice(0, 5)
  return (
    <section className="env-card glass-panel" aria-labelledby="environment-related-heading">
      <h2 id="environment-related-heading" className="env-card-title">Related</h2>
      <h3 className="env-card-subtitle">Environment context</h3>
      {prompts.isPending ? <p className="env-muted">Loading prompts…</p>
        : prompts.isError ? <p className="env-muted">Prompts couldn’t be loaded.</p>
        : prompts.data.length === 0 ? <p className="env-muted">No environment-context prompts describe this environment yet.</p>
        : <ul className="env-related-list">{prompts.data.map(prompt => (
          <li key={prompt.id}><Link to={`/prompts?ns=environment&prompt=${encodeURIComponent(prompt.id)}`}>{prompt.description?.trim() || prompt.key}</Link>{prompt.active_version && <span className="env-muted"> · v{prompt.active_version.version}</span>}</li>
        ))}</ul>}
      {canAddContext && <Link className="btn btn-ghost btn-sm env-related-action" to={`/prompts?ns=environment&new=environment&environment_id=${encodeURIComponent(environment.id)}`}>Add environment context</Link>}
      <h3 className="env-card-subtitle">Recent tests</h3>
      {!projectId && !projectPending ? <p className="env-muted">This environment’s application isn’t visible to you, so its tests can’t be listed.</p>
        : !projectId || recent.isPending ? <p className="env-muted">Loading tests…</p>
        : recent.isError ? <p className="env-muted">Tests couldn’t be loaded.</p>
        : tests.length === 0 ? <p className="env-muted">None of the latest 100 tests in this project used this environment.</p>
        : <ul className="env-related-list">{tests.map(test => (
          <li key={test.thread_id}><Link to={`/runs/${encodeURIComponent(test.thread_id)}`}>{test.title || 'Untitled test'}</Link>{test.updated_at && <span className="env-muted"> · {formatRelative(test.updated_at)}</span>}</li>
        ))}</ul>}
    </section>
  )
}

export function EnvironmentDetailPage() {
  useTimestampPreference()
  const { id = '' } = useParams<{ id: string }>()
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const environment = useEnvironment(id)
  const writeCount = usePendingMutationCount(environmentWriteMutationKey(id))
  // Archived applications included: their environments still open here.
  const applications = useApplicationsIndex({ includeArchived: true })
  const environments = useEnvironmentsIndex()
  const labels = useProjectLabels()
  const rescan = useRescanEnvironment(id)
  const consumer = useConsumer()
  const owningApplication = applications.data?.find((application) => application.id === environment.data?.application_id)
  const canMutate = canMutateAudience(consumer, owningApplication?.project_id, owningApplication?.id)
  const canApproveTarget = isGlobalAdmin(consumer)
  // ?edit=1 lets the list's row menu land directly in edit mode.
  const editRequested = canMutate && searchParams.get('edit') === '1'
  const [editing, setEditing] = useState(editRequested)
  const [deleting, setDeleting] = useState(false)
  const activeEnvironmentIdRef = useRef(id)
  activeEnvironmentIdRef.current = id
  const resetRescan = rescan.reset
  // The page is about this environment: name it in the topbar.
  useTopbarTitle(environment.data?.name ?? null)

  useEffect(() => {
    setEditing(editRequested)
    resetRescan()
  }, [id, editRequested, resetRescan])

  if (environment.isPending) {
    return (
      <Skeleton label="Loading environment" rows={2} />
    )
  }

  if (!environment.data) {
    return (
      <ProblemCard
        title="Environment unavailable"
        message={environment.error?.message ?? 'The environment could not be loaded.'}
        onRetry={() => void environment.refetch()}
      />
    )
  }

  const env = environment.data
  const app = applications.data?.find((candidate) => candidate.id === env.application_id)
  const projectName = app ? (!labels.isError && labels.data?.find(row => row.project_id === app.project_id)?.name) || app.project_id : null
  const siblingNames = (environments.data ?? []).filter(other => other.application_id === env.application_id && other.id !== env.id).map(other => other.name)

  return (
    <section className="env-page animate-enter">
      {environment.isError && (
        <CachedDataWarning error={environment.error} onRetry={() => void environment.refetch()} />
      )}
      <header className="env-detail-header glass-panel">
        <div className="env-detail-heading">
          <nav className="env-breadcrumb" aria-label="Breadcrumb">
            <Link to="/environments">Applications &amp; environments</Link>
            {app && (
              <>
                <span className="env-breadcrumb-sep" aria-hidden="true">/</span>
                <Link to={`/environments?project=${encodeURIComponent(app.project_id)}`}>{projectName}</Link>
                <span className="env-breadcrumb-sep" aria-hidden="true">/</span>
                <Link to={`/environments?application=${encodeURIComponent(app.id)}`}>{app.name}</Link>
              </>
            )}
          </nav>
          <div className="env-detail-title-row">
            <TargetBadge environment={env} />
            {kindLabel(env.kind) && <span className="env-muted">{kindLabel(env.kind)}</span>}
          </div>
        </div>
        {canMutate && (
          <div className="env-detail-actions">
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => setEditing((value) => !value)} disabled={writeCount > 0}>
              {editing ? 'Close editor' : 'Edit'}
            </button>
            <button type="button" className="btn btn-ghost btn-sm env-danger-text" onClick={() => setDeleting(true)} disabled={writeCount > 0}>Delete…</button>
          </div>
        )}
      </header>

      <div className="env-detail-panels">
        <EnvironmentTarget key={`target:${env.id}`} environment={env} canApprove={canApproveTarget} onEdit={() => setEditing(true)} />
        {editing && canMutate ? (
          <EditEnvironmentForm
            key={env.id}
            environment={env}
            siblingNames={siblingNames}
            canApproveTarget={canApproveTarget}
            onDone={(completedEnvironmentId) => {
              if (activeEnvironmentIdRef.current === completedEnvironmentId) setEditing(false)
            }}
          />
        ) : (
          <ReferenceCard environment={env} />
        )}
        <InventoryPanel
          environmentId={env.id}
          canMutate={canMutate}
          rescan={rescan}
          writePending={writeCount > 0}
        />
        <RelatedCard environment={env} projectId={app?.project_id} projectPending={applications.isPending} canAddContext={canApproveTarget} />
      </div>
      {deleting && (
        <DeleteEnvironmentDialog environment={env} onClose={() => setDeleting(false)}
          onDeleted={() => void navigate(app ? `/environments?application=${encodeURIComponent(app.id)}` : '/environments')} />
      )}
    </section>
  )
}
