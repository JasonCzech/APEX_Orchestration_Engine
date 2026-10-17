import { useTimestampPreference } from '@/theme/preferences'
import { TableToolbar } from '@/components/ux/TableToolbar'
import { TabList, TabPanels } from '@/components/ux/TabList'
import { Skeleton } from '@/components/ux/Skeleton'
import { StateCard } from '@/components/ux/StateCard'
/**
 * /admin/connections — the adapter connection registry, searchable by its
 * operational identity and scope. Admin-gated.
 *
 * Create-time 422s carry the registered-provider list in the problem detail
 * ("unknown provider 'x' for kind 'y'; registered providers: a, b") — the
 * panel surfaces that message verbatim as an inline error, never a toast.
 */
import { useId, useState, type FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'

import {
  PORT_KINDS,
  connectionWriteMutationKey,
  useConnectionsIndex,
  useCreateConnection,
  useSetConnectionEnabled,
  type Connection,
  type PortKind,
} from '@/api/hooks/useConnections'
import { usePendingMutationCount } from '@/api/hooks/usePendingMutationCount'
import { useConsumer } from '@/auth/AuthProvider'
import { hasFullProjectScope, isGlobalAdmin } from '@/auth/RequireRole'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { ProblemCard } from '@/components/ProblemCard'
import { formatRelative } from '@/utils/time'

import { parseJsonObject } from './adminLogic'
import { AdminGate, TogglePill } from './adminShared'
import { scopedConnectionPolicyIssue } from './connectionPolicy'
import { ProviderConnectionFields } from './ProviderConnectionFields'
import {
  buildOptions,
  emptyDraft,
  guidedBaseUrl,
  guidedIssue,
  providerSpec,
  providersForKind,
  type OptionsDraft,
} from './providerCatalog'
import './admin.css'

const SKELETON_ROWS = 4
const APEX_LOAD_PROVIDER = 'apex_load'

type RegistryView = 'connections' | 'apex-load'

function isRegistryView(value: string | null): value is RegistryView {
  return value === 'connections' || value === 'apex-load'
}

function isApexLoadConnection(connection: Connection): boolean {
  return connection.kind === 'execution_engine' && connection.provider === APEX_LOAD_PROVIDER
}

function ConnectionRow({ connection }: { connection: Connection }) {
  const setEnabled = useSetConnectionEnabled(connection.id)
  const writeCount = usePendingMutationCount(connectionWriteMutationKey(connection.id))

  return (
    <tr data-testid={`conn-row-${connection.id}`}>
      <td>
        <Link
          className="adm-card-link strong"
          to={`/admin/connections/${connection.id}`}
        >
          {connection.name}
        </Link>
      </td>
      <td>{connection.kind.replaceAll('_', ' ')}</td>
      <td>
        <span className="dash-context-chip">{connection.provider}</span>
      </td>
      <td>
        <span className="dash-context-chip adm-chip-muted">
          {connection.project_id ?? 'global'}
        </span>
      </td>
      <td>
        <TogglePill
          enabled={connection.enabled}
          label={`Toggle ${connection.name}`}
          pending={writeCount > 0}
          onToggle={() =>
            setEnabled.mutate({ connectionId: connection.id, enabled: !connection.enabled })
          }
        />
      </td>
      <td className="adm-time" title={connection.updated_at}>
        {formatRelative(connection.updated_at)}
      </td>
    </tr>
  )
}

/** Inline create panel — kind, provider, name, project, base_url, options, secret_ref. */
function CreateConnectionPanel({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate()
  const consumer = useConsumer()
  const globalAdmin = isGlobalAdmin(consumer)
  const projectScopes = Array.from(
    new Set(
      (consumer?.scopes ?? [])
        .filter((scope) => hasFullProjectScope(consumer, scope.project_id))
        .map((scope) => scope.project_id),
    ),
  )
  const allowedKinds = globalAdmin ? PORT_KINDS : PORT_KINDS.filter((value) => value !== 'secrets')
  const create = useCreateConnection()
  const [kind, setKind] = useState<PortKind>('work_tracking')
  const [provider, setProvider] = useState('')
  const [customProvider, setCustomProvider] = useState(providersForKind('work_tracking').length === 0)
  const [name, setName] = useState('')
  const [project, setProject] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [draft, setDraft] = useState<OptionsDraft>(emptyDraft)
  const [secretRef, setSecretRef] = useState('')

  const spec = customProvider ? null : providerSpec(kind, provider)
  const guided = spec !== null && draft.mode === 'guided'
  const rawParse = parseJsonObject(draft.rawText)
  const options = guided ? buildOptions(spec, draft) : rawParse.ok ? rawParse.value : null
  const missingField = guided ? guidedIssue(spec, draft) : null
  // A guided base-URL field owns the column too: adapters read base_url from
  // options (the server backfills it from the column only when options lacks
  // it), so submitting both keeps the display column and the effective value
  // from ever diverging.
  const mirroredBaseUrl = guided ? guidedBaseUrl(spec, draft) : null
  const ownsBaseUrl = guided && spec.optionFields.some((field) => field.mirrorsBaseUrl)
  const policyIssue =
    !globalAdmin && options !== null ? scopedConnectionPolicyIssue(kind, provider, options) : null
  const canSubmit =
    provider.trim() !== '' &&
    name.trim() !== '' &&
    options !== null &&
    missingField === null &&
    (globalAdmin || (projectScopes.includes(project) && policyIssue === null)) &&
    !create.isPending

  function changeKind(next: PortKind) {
    setKind(next)
    setProvider('')
    setCustomProvider(providersForKind(next).length === 0)
    setDraft(emptyDraft())
  }

  function changeProvider(next: string, custom: boolean) {
    setProvider(next)
    setCustomProvider(custom)
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSubmit || options === null) return
    create.mutate(
      {
        kind,
        provider: provider.trim(),
        name: name.trim(),
        project_id: project.trim() || null,
        base_url: (ownsBaseUrl ? mirroredBaseUrl : baseUrl.trim() || null) ?? null,
        options,
        secret_ref: globalAdmin ? secretRef.trim() || null : null,
      },
      {
        // Land on the detail page with the probe already running: a connection
        // that cannot reach its provider should say so before anyone uses it.
        onSuccess: (created) => void navigate(`/admin/connections/${created.id}?test=1`),
      },
    )
  }

  return (
    <form className="glass-panel adm-panel" onSubmit={submit} aria-label="New connection">
      <h2 className="adm-panel-title">New connection</h2>
      <div className="adm-form-grid">
        <label className="adm-field">
          <span className="adm-field-label">Kind</span>
          <select
            className="field-select"
            aria-label="Kind"
            value={kind}
            onChange={(event) => changeKind(event.target.value as PortKind)}
          >
            {allowedKinds.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
        <ProviderConnectionFields
          kind={kind}
          provider={provider}
          customProvider={customProvider}
          onProviderChange={changeProvider}
          draft={draft}
          onDraftChange={setDraft}
        />
        <label className="adm-field">
          <span className="adm-field-label">Name</span>
          <input
            type="text"
            className="field-input"
            aria-label="Name"
            placeholder="jira-prod"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label className="adm-field">
          <span className="adm-field-label">Project{globalAdmin ? ' (optional)' : ''}</span>
          {globalAdmin ? <input
            type="text"
            className="field-input"
            aria-label="Project"
            placeholder="leave empty for global"
            value={project}
            onChange={(event) => setProject(event.target.value)}
          /> : <select
            className="field-select"
            aria-label="Project"
            value={project}
            onChange={(event) => setProject(event.target.value)}
          >
            <option value="">Select a project…</option>
            {projectScopes.map((projectId) => <option key={projectId} value={projectId}>{projectId}</option>)}
          </select>}
        </label>
        {!ownsBaseUrl && <label className="adm-field">
          <span className="adm-field-label">Base URL (optional)</span>
          <input
            type="text"
            className="field-input"
            aria-label="Base URL"
            placeholder="https://jira.example.com"
            value={baseUrl}
            onChange={(event) => setBaseUrl(event.target.value)}
          />
        </label>}
        {globalAdmin && <label className="adm-field">
          <span className="adm-field-label">Secret ref</span>
          <input
            type="text"
            className="field-input"
            aria-label="Secret ref"
            placeholder={spec?.secretRef?.placeholder ?? 'env:APEX_INTEGRATION_TOKEN'}
            value={secretRef}
            onChange={(event) => setSecretRef(event.target.value)}
          />
          <span className="adm-field-help">
            {spec?.secretRef?.help ?? 'env:NAME — references only, never raw secrets'}
          </span>
        </label>}
      </div>
      {options === null && (
        <p className="adm-form-error" role="alert">
          {rawParse.ok ? 'Options must be a JSON object.' : rawParse.message}
        </p>
      )}
      {missingField && (
        <p className="adm-form-error" role="alert">
          {missingField}
        </p>
      )}
      {policyIssue && (
        <p className="adm-form-error" role="alert">
          {policyIssue}
        </p>
      )}
      {create.isError && (
        <div className="adm-inline-error" role="alert">
          <span>{create.error.message}</span>
        </div>
      )}
      <div className="adm-panel-actions">
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={onClose}
          disabled={create.isPending}
        >
          Cancel
        </button>
        <button type="submit" className="btn btn-primary btn-sm" disabled={!canSubmit}>
          {create.isPending ? 'Creating…' : 'Create connection'}
        </button>
      </div>
    </form>
  )
}

function ConnectionsContent() {
  const tabsId = useId()
  const connections = useConnectionsIndex()
  const [creating, setCreating] = useState(false)
  const [query, setQuery] = useState('')
  const [searchParams, setSearchParams] = useSearchParams()
  const rawView = searchParams.get('view')
  const view: RegistryView = isRegistryView(rawView) ? rawView : 'connections'
  const normalizedQuery = query.trim().toLowerCase()
  const filteredConnections = (connections.data ?? []).filter((connection) =>
    [connection.name, connection.kind, connection.provider, connection.project_id ?? 'global']
      .some((value) => value.toLowerCase().includes(normalizedQuery)),
  )
  const apexLoadConnections = (connections.data ?? []).filter(isApexLoadConnection)

  function selectView(next: RegistryView) {
    const params = new URLSearchParams(searchParams)
    if (next === 'connections') params.delete('view')
    else params.set('view', next)
    setSearchParams(params)
  }

  return (
    <section className="adm-page animate-enter">
      <header className="adm-toolbar glass-panel">
        <div>
          <p className="adm-field-help">Enabled controls whether Orchestration may use a connection; it is not a health check.</p>
        </div>
        {!creating && (
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={() => setCreating(true)}
          >
            New connection
          </button>
        )}
      </header>

      <TabList id={tabsId} label="Connection views" value={view} onChange={selectView}
        items={[{ id: 'connections', label: 'All connections' }, { id: 'apex-load', label: 'APEX Load' }]} />

      {creating && <CreateConnectionPanel onClose={() => setCreating(false)} />}

      <TabPanels id={tabsId} items={['connections', 'apex-load']} value={view}>
      {view === 'connections' && !connections.isPending && !!connections.data?.length && (
        <TableToolbar label="Connection filters" filterCount={Number(Boolean(query.trim()))} onClear={() => setQuery('')}><label className="adm-registry-search">
          <span className="sr-only">Filter connections</span>
          <input
            type="search"
            className="field-input"
            placeholder="Filter by name, kind, provider, or project"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label></TableToolbar>
      )}

      {connections.isError && connections.data && (
        <CachedDataWarning error={connections.error} onRetry={() => void connections.refetch()} />
      )}

      {connections.isPending ? (
        <Skeleton label="Loading connections" rows={SKELETON_ROWS} />
      ) : connections.isError && !connections.data ? (
        <ProblemCard
          title="Connections unavailable"
          message={connections.error.message}
          onRetry={() => void connections.refetch()}
        />
      ) : connections.data.length === 0 ? (
        <StateCard kind="empty" title="No connections yet" action={creating ? null : <button className="btn btn-primary" onClick={() => setCreating(true)}>Create connection</button>}>
          Connections bind work tracking, logs, and execution engines to provider adapters.
        </StateCard>
      ) : view === 'apex-load' && apexLoadConnections.length === 0 ? (
        <StateCard kind="unconfigured" title="No APEX Load connections yet" action={creating ? null : <button className="btn btn-primary" onClick={() => setCreating(true)}>Create connection</button>}>
          Create an enabled APEX Load execution-engine connection to configure a project workspace.
        </StateCard>
      ) : view === 'connections' && filteredConnections.length === 0 ? (
        <StateCard kind="filtered" title="No matching connections" action={<button className="btn btn-secondary" onClick={() => setQuery('')}>Clear filter</button>}>
          Try a different name, provider, kind, or project.
        </StateCard>
      ) : view === 'apex-load' ? (
        <section className="glass-panel adm-card-panel" aria-label="APEX Load workspaces">
          <h2 className="adm-panel-title">APEX Load workspaces</h2>
          <p className="adm-field-help">
            Configure credentials and scope on the connection. Open a workspace to manage scenarios,
            schedules, executions, and evidence for that connection.
          </p>
          <div className="data-table-wrap">
            <table className="data-table striped adm-connections-table" aria-label="APEX Load connections">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Scope</th>
                  <th>Enabled</th>
                  <th><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {apexLoadConnections.map((connection) => (
                  <tr key={connection.id} data-testid={`apex-load-connection-${connection.id}`}>
                    <td><Link className="adm-card-link strong" to={`/admin/connections/${connection.id}`}>{connection.name}</Link></td>
                    <td><span className="dash-context-chip adm-chip-muted">{connection.project_id ?? 'global'}</span></td>
                    <td>
                      <span className={connection.enabled ? 'status-badge success' : 'status-badge danger'}>
                        {connection.enabled ? 'Enabled' : 'Disabled'}
                      </span>
                    </td>
                    <td>
                      {connection.enabled ? (
                        <Link className="btn btn-secondary btn-sm" to={`/apex-load?connection=${encodeURIComponent(connection.id)}`}>
                          Open workspace
                        </Link>
                      ) : (
                        <span className="adm-muted">Enable this connection to open its workspace.</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : (
        <div className="data-table-wrap">
          <table className="data-table striped adm-connections-table" aria-label="Connections">
            <thead>
              <tr>
                <th>Name</th>
                <th>Kind</th>
                <th>Provider</th>
                <th>Scope</th>
                <th>Enabled</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {filteredConnections.map((connection) => (
                <ConnectionRow key={connection.id} connection={connection} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      </TabPanels>
    </section>
  )
}

export function ConnectionsPage() {
  useTimestampPreference()
  return (
    <AdminGate>
      <ConnectionsContent />
    </AdminGate>
  )
}
