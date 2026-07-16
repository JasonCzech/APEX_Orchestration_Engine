/**
 * /admin/connections/:id?tab=config|host-mappings (plan Part 2 route table +
 * UX 2.f, D7). Admin-gated.
 *
 * Header carries the lifecycle actions: enabled toggle (enable/disable
 * endpoints), [Test connection] (POST test, always 200 — ok/fail render as an
 * inline result panel, deliberately NOT a toast) and type-to-confirm delete.
 * Config tab PATCHes the mutable fields (kind shown immutable); host-mappings
 * tab PUTs the FULL mapping list on save.
 */
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'

import { useMutationState } from '@tanstack/react-query'

import {
  connectionProbeMutationKey,
  connectionWriteMutationKey,
  useConnection,
  useDeleteConnection,
  useHostMappings,
  usePutHostMappings,
  useSetConnectionEnabled,
  useTestConnection,
  useUpdateConnection,
  type Connection,
  type HostMappingOut,
} from '@/api/hooks/useConnections'
import { usePendingMutationCount } from '@/api/hooks/usePendingMutationCount'
import { useConsumer } from '@/auth/AuthProvider'
import { hasFullProjectScope, isGlobalAdmin } from '@/auth/RequireRole'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { Dialog } from '@/components/Dialog'
import { ProblemCard } from '@/components/ProblemCard'

import { kindLabel, parseJsonObject } from './adminLogic'
import { AdminGate, TogglePill } from './adminShared'
import {
  isRuntimeIdentityKind,
  scopedConnectionPolicyIssue,
} from './connectionPolicy'
import './admin.css'

const TABS = ['config', 'host-mappings'] as const
type DetailTab = (typeof TABS)[number]

const TAB_LABELS: Record<DetailTab, string> = {
  config: 'Config',
  'host-mappings': 'Host mappings',
}

function isDetailTab(value: string | null): value is DetailTab {
  return value === 'config' || value === 'host-mappings'
}

/** Inline probe result — green w/ latency on ok, danger w/ adapter detail on fail. */
function ProbePanel({ probe }: { probe: ReturnType<typeof useTestConnection> }) {
  if (probe.isError) {
    return (
      <div className="adm-inline-error" role="alert" data-testid="probe-result">
        <span>Test failed: {probe.error.message}</span>
      </div>
    )
  }
  if (!probe.data) return null
  if (probe.data.ok) {
    return (
      <div className="adm-inline-ok" role="status" data-testid="probe-result">
        <span className="status-badge success">OK</span>
        <span>
          Connection healthy in {Math.round(probe.data.latency_ms)} ms — {probe.data.detail}
        </span>
      </div>
    )
  }
  return (
    <div className="adm-inline-error" role="alert" data-testid="probe-result">
      <span className="status-badge danger">FAIL</span>
      <span>{probe.data.detail}</span>
    </div>
  )
}

/** Config tab: PATCH form. Kind is immutable and rendered read-only. */
function ConfigTab({ connection }: { connection: Connection }) {
  const consumer = useConsumer()
  const globalAdmin = isGlobalAdmin(consumer)
  const projectScopes = Array.from(
    new Set(
      (consumer?.scopes ?? [])
        .filter((scope) => hasFullProjectScope(consumer, scope.project_id))
        .map((scope) => scope.project_id),
    ),
  )
  const update = useUpdateConnection(connection.id)
  const writeCount = usePendingMutationCount(connectionWriteMutationKey(connection.id))
  const [name, setName] = useState(connection.name)
  const [provider, setProvider] = useState(connection.provider)
  const [project, setProject] = useState(connection.project_id ?? '')
  const [baseUrl, setBaseUrl] = useState(connection.base_url ?? '')
  const [secretRef, setSecretRef] = useState(connection.secret_ref ?? '')
  const [optionsText, setOptionsText] = useState(() =>
    JSON.stringify(connection.options, null, 2),
  )

  const runtimeIdentity = isRuntimeIdentityKind(connection.kind)
  const optionsParse = parseJsonObject(optionsText)
  const policyIssue =
    !globalAdmin && !runtimeIdentity && optionsParse.ok
      ? scopedConnectionPolicyIssue(connection.kind, provider, optionsParse.value)
      : null
  const projectAllowed = runtimeIdentity || globalAdmin || projectScopes.includes(project)
  const canSave =
    name.trim() !== '' &&
    (runtimeIdentity ||
      (provider.trim() !== '' &&
        optionsParse.ok &&
        projectAllowed &&
        (globalAdmin || policyIssue === null))) &&
    writeCount === 0

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSave) return
    if (runtimeIdentity) {
      update.mutate({ connectionId: connection.id, body: { name: name.trim() } })
      return
    }
    if (!optionsParse.ok) return
    update.mutate({
      connectionId: connection.id,
      body: {
        name: name.trim(),
        provider: provider.trim(),
        project_id: project.trim() || null,
        base_url: baseUrl.trim() || null,
        ...(globalAdmin ? { secret_ref: secretRef.trim() || null } : {}),
        options: optionsParse.value,
      },
    })
  }

  return (
    <form className="adm-card-panel glass-panel" onSubmit={submit} aria-label="Connection config">
      <div className="adm-form-grid">
        <div className="adm-field">
          <span className="adm-field-label">Kind (immutable)</span>
          <span className="dash-context-chip adm-kind-chip">{connection.kind}</span>
        </div>
        <label className="adm-field">
          <span className="adm-field-label">Provider</span>
          <input
            type="text"
            className="field-input"
            aria-label="Provider"
            value={provider}
            readOnly={runtimeIdentity}
            onChange={(event) => setProvider(event.target.value)}
          />
          {!runtimeIdentity && (
            <span className="adm-field-help">must be a registered provider</span>
          )}
        </label>
        <label className="adm-field">
          <span className="adm-field-label">Name</span>
          <input
            type="text"
            className="field-input"
            aria-label="Name"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label className="adm-field">
          <span className="adm-field-label">Project (optional)</span>
          {runtimeIdentity ? <input
            type="text"
            className="field-input"
            aria-label="Project"
            value={project || 'global'}
            readOnly
          /> : globalAdmin ? <input
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
            {projectScopes.map((projectId) => (
              <option key={projectId} value={projectId}>{projectId}</option>
            ))}
          </select>}
        </label>
        <label className="adm-field">
          <span className="adm-field-label">Base URL (optional)</span>
          <input
            type="text"
            className="field-input"
            aria-label="Base URL"
            value={baseUrl}
            readOnly={runtimeIdentity}
            onChange={(event) => setBaseUrl(event.target.value)}
          />
        </label>
        {globalAdmin && <label className="adm-field">
          <span className="adm-field-label">Secret ref</span>
          <input
            type="text"
            className="field-input"
            aria-label="Secret ref"
            placeholder="env:JIRA_API_TOKEN"
            value={secretRef}
            readOnly={runtimeIdentity}
            onChange={(event) => setSecretRef(event.target.value)}
          />
          <span className="adm-field-help">env:NAME — references only, never raw secrets</span>
        </label>}
      </div>
      <label className="adm-field">
        <span className="adm-field-label">Options (JSON)</span>
        <textarea
          className="field-input adm-json-input"
          aria-label="Options JSON"
          rows={6}
          spellCheck={false}
          value={optionsText}
          readOnly={runtimeIdentity}
          onChange={(event) => setOptionsText(event.target.value)}
        />
      </label>
      {runtimeIdentity && (
        <p className="adm-field-help">
          Runtime identity fields are immutable. Create a replacement connection to change its
          provider, project, endpoint, secret, or options.
        </p>
      )}
      {!runtimeIdentity && !optionsParse.ok && (
        <p className="adm-form-error" role="alert">
          {optionsParse.message}
        </p>
      )}
      {policyIssue && (
        <p className="adm-form-error" role="alert">
          {policyIssue}
        </p>
      )}
      {update.isError && (
        <div className="adm-inline-error" role="alert">
          <span>{update.error.message}</span>
        </div>
      )}
      <div className="adm-panel-actions">
        <button type="submit" className="btn btn-primary btn-sm" disabled={!canSave}>
          {update.isPending ? 'Saving…' : 'Save changes'}
        </button>
      </div>
    </form>
  )
}

interface MappingDraft {
  pattern: string
  target: string
  enabled: boolean
}

/** Editable mapping rows; Save replaces the FULL list (PUT semantics). */
function HostMappingsEditor({
  connectionId,
  initial,
}: {
  connectionId: string
  initial: HostMappingOut[]
}) {
  const put = usePutHostMappings(connectionId)
  const writeCount = usePendingMutationCount(connectionWriteMutationKey(connectionId))
  const writePending = writeCount > 0
  const generationRef = useRef<string | null>(connectionId)
  const [rows, setRows] = useState<MappingDraft[]>(() =>
    initial.map(({ pattern, target, enabled }) => ({ pattern, target, enabled })),
  )
  const [isDirty, setIsDirty] = useState(false)

  useEffect(
    () => () => {
      generationRef.current = null
    },
    [],
  )

  useEffect(() => {
    // Connection lifecycle mutations invalidate the broad connections cache.
    // Accept a refreshed server snapshot only while the editor is pristine;
    // otherwise an unrelated enable/disable refetch would erase local edits.
    if (!isDirty) {
      setRows(initial.map(({ pattern, target, enabled }) => ({ pattern, target, enabled })))
    }
  }, [initial, isDirty])

  function patchRow(index: number, patch: Partial<MappingDraft>) {
    setIsDirty(true)
    setRows((current) => current.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  }

  const canSave = rows.every((row) => row.pattern.trim() !== '' && row.target.trim() !== '')

  return (
    <div className="adm-card-panel glass-panel">
      {rows.length === 0 ? (
        <p className="adm-muted">No host mappings. Requests pass through unmapped.</p>
      ) : (
        <div className="data-table-wrap">
          <table className="data-table adm-mappings-table" aria-label="Host mappings">
            <thead>
              <tr>
                <th>Pattern</th>
                <th>Target</th>
                <th>Enabled</th>
                <th>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={index}>
                  <td>
                    <input
                      type="text"
                      className="field-input"
                      aria-label={`Mapping ${index + 1} pattern`}
                      value={row.pattern}
                      disabled={writePending}
                      onChange={(event) => patchRow(index, { pattern: event.target.value })}
                    />
                  </td>
                  <td>
                    <input
                      type="text"
                      className="field-input"
                      aria-label={`Mapping ${index + 1} target`}
                      value={row.target}
                      disabled={writePending}
                      onChange={(event) => patchRow(index, { target: event.target.value })}
                    />
                  </td>
                  <td className="adm-mapping-enabled">
                    <input
                      type="checkbox"
                      aria-label={`Mapping ${index + 1} enabled`}
                      checked={row.enabled}
                      disabled={writePending}
                      onChange={(event) => patchRow(index, { enabled: event.target.checked })}
                    />
                  </td>
                  <td className="adm-mapping-remove">
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                    aria-label={`Remove mapping ${index + 1}`}
                      onClick={() => {
                        setIsDirty(true)
                        setRows((current) => current.filter((_, i) => i !== index))
                      }}
                      disabled={writePending}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {put.isError && (
        <div className="adm-inline-error" role="alert">
          <span>{put.error.message}</span>
        </div>
      )}
      <div className="adm-panel-actions adm-mappings-actions">
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          onClick={() => {
            setIsDirty(true)
            setRows((current) => [...current, { pattern: '', target: '', enabled: true }])
          }}
          disabled={writePending}
        >
          Add mapping
        </button>
        <button
          type="button"
          className="btn btn-primary btn-sm"
          disabled={!canSave || writePending}
          onClick={() =>
            put.mutate({
              connectionId,
              mappings: rows.map((row) => ({
                pattern: row.pattern.trim(),
                target: row.target.trim(),
                enabled: row.enabled,
              })),
            }, {
              onSuccess: (saved) => {
                if (generationRef.current !== connectionId) return
                setRows(
                  saved.map(({ pattern, target, enabled }) => ({ pattern, target, enabled })),
                )
                setIsDirty(false)
              },
            })
          }
        >
          {put.isPending ? 'Saving…' : 'Save mappings'}
        </button>
      </div>
    </div>
  )
}

function HostMappingsTab({ connectionId }: { connectionId: string }) {
  const mappings = useHostMappings(connectionId)

  if (mappings.isPending) {
    return (
      <div role="status" aria-busy="true" aria-label="Loading host mappings" className="adm-muted">
        Loading host mappings…
      </div>
    )
  }
  if (!mappings.data) {
    return (
      <ProblemCard
        title="Host mappings unavailable"
        message={mappings.error?.message ?? 'Host mappings could not be loaded.'}
        onRetry={() => void mappings.refetch()}
      />
    )
  }
  return (
    <>
      {mappings.isError && (
        <CachedDataWarning error={mappings.error} onRetry={() => void mappings.refetch()} />
      )}
      <HostMappingsEditor connectionId={connectionId} initial={mappings.data} />
    </>
  )
}

/** Type-to-confirm delete modal (mirrors the environments pattern). */
function DeleteConnectionModal({
  connection,
  onClose,
}: {
  connection: Connection
  onClose: () => void
}) {
  const navigate = useNavigate()
  const remove = useDeleteConnection(connection.id)
  const writeCount = usePendingMutationCount(connectionWriteMutationKey(connection.id))
  const generationRef = useRef<string | null>(connection.id)
  const [confirmation, setConfirmation] = useState('')
  const canDelete = confirmation === connection.name && writeCount === 0

  useEffect(
    () => () => {
      generationRef.current = null
    },
    [],
  )

  function close() {
    if (remove.isPending) return
    onClose()
  }

  return (
    <Dialog
      overlayClassName="adm-overlay"
      className="adm-modal glass-panel"
      ariaLabel={`Delete connection ${connection.name}`}
      onClose={close}
    >
      <h2 className="adm-panel-title">Delete connection</h2>
        <p className="adm-modal-caption">
          This permanently removes <strong>{connection.name}</strong> and its host mappings. Type
          the connection name to confirm.
        </p>
        <input
          type="text"
          className="field-input"
          aria-label="Type the connection name to confirm"
          placeholder={connection.name}
          value={confirmation}
          onChange={(event) => setConfirmation(event.target.value)}
        />
        {remove.isError && (
          <div className="adm-inline-error" role="alert">
            <span>Delete failed: {remove.error.message}</span>
          </div>
        )}
        <div className="adm-panel-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={close}
            disabled={remove.isPending}
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-danger btn-sm"
            disabled={!canDelete}
            onClick={() =>
              remove.mutate(connection.id, {
                onSuccess: () => {
                  if (generationRef.current === connection.id) void navigate('/admin/connections')
                },
              })
            }
          >
            {remove.isPending ? 'Deleting…' : 'Delete connection'}
          </button>
        </div>
    </Dialog>
  )
}

function ConnectionDetailContent({ connectionId }: { connectionId: string }) {
  const id = connectionId
  const [searchParams, setSearchParams] = useSearchParams()
  const connection = useConnection(id)
  const setEnabled = useSetConnectionEnabled(id)
  const writeCount = usePendingMutationCount(connectionWriteMutationKey(id))
  const writeMutationIds = useMutationState({
    filters: { mutationKey: connectionWriteMutationKey(id) },
    select: (mutation) => mutation.mutationId,
  })
  const probeMutationIds = useMutationState({
    filters: { mutationKey: connectionProbeMutationKey(id) },
    select: (mutation) => mutation.mutationId,
  })
  const latestWriteMutationIdRef = useRef(0)
  latestWriteMutationIdRef.current = Math.max(
    latestWriteMutationIdRef.current,
    0,
    ...writeMutationIds,
  )
  const latestProbeMutationId = Math.max(0, ...probeMutationIds)
  const probeIsCurrent = latestProbeMutationId > latestWriteMutationIdRef.current
  const probe = useTestConnection(id)
  const [deleting, setDeleting] = useState(false)

  const rawTab = searchParams.get('tab')
  const tab: DetailTab = isDetailTab(rawTab) ? rawTab : 'config'

  function selectTab(next: DetailTab) {
    const params = new URLSearchParams(searchParams)
    params.set('tab', next)
    setSearchParams(params)
  }

  if (connection.isPending) {
    return (
      <div
        className="adm-skeleton animate-enter"
        role="status"
        aria-busy="true"
        aria-label="Loading connection"
      >
        <div className="glass-panel adm-skeleton-card" />
        <div className="glass-panel adm-skeleton-card" />
      </div>
    )
  }

  if (!connection.data) {
    return (
      <ProblemCard
        title="Connection unavailable"
        message={connection.error?.message ?? 'The connection could not be loaded.'}
        onRetry={() => void connection.refetch()}
      />
    )
  }

  const conn = connection.data

  return (
    <section className="adm-page animate-enter">
      {connection.isError && (
        <CachedDataWarning error={connection.error} onRetry={() => void connection.refetch()} />
      )}
      <header className="adm-detail-header glass-panel">
        <div className="adm-detail-heading">
          <nav className="adm-breadcrumb" aria-label="Breadcrumb">
            <Link to="/admin/connections">Connections</Link>
            <span className="adm-breadcrumb-sep">/</span>
            <span>{kindLabel(conn.kind)}</span>
          </nav>
          <div className="adm-detail-title-row">
            {/* The topbar already renders the route-handle h1. */}
            <h2 className="adm-detail-title">{conn.name}</h2>
            <span className="dash-context-chip adm-kind-chip">{conn.kind}</span>
            <span className="dash-context-chip">{conn.provider}</span>
            <TogglePill
              enabled={conn.enabled}
              label={`Toggle ${conn.name}`}
              pending={writeCount > 0}
              onToggle={() => setEnabled.mutate({ connectionId: conn.id, enabled: !conn.enabled })}
            />
          </div>
        </div>
        <div className="adm-detail-actions">
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => probe.mutate()}
            disabled={probe.isPending || writeCount > 0}
          >
            {probe.isPending ? 'Testing…' : 'Test connection'}
          </button>
          <button
            type="button"
            className="btn btn-danger btn-sm"
            onClick={() => setDeleting(true)}
            disabled={writeCount > 0}
          >
            Delete
          </button>
        </div>
      </header>

      {probeIsCurrent && <ProbePanel probe={probe} />}

      <div className="adm-tabs" role="tablist" aria-label="Connection sections">
        {TABS.map((entry) => (
          <button
            key={entry}
            type="button"
            role="tab"
            aria-selected={tab === entry}
            className={`adm-tab${tab === entry ? ' active' : ''}`}
            onClick={() => selectTab(entry)}
          >
            {TAB_LABELS[entry]}
          </button>
        ))}
      </div>

      {tab === 'config' ? (
        // ConfigTab preserves dirty edits while lifecycle mutations refetch.
        <ConfigTab key={`config:${conn.id}`} connection={conn} />
      ) : (
        <HostMappingsTab key={`mappings:${conn.id}`} connectionId={conn.id} />
      )}

      {deleting && (
        <DeleteConnectionModal
          key={`delete:${conn.id}`}
          connection={conn}
          onClose={() => setDeleting(false)}
        />
      )}
    </section>
  )
}

function ConnectionDetailRoute() {
  const { id = '' } = useParams<{ id: string }>()
  return <ConnectionDetailContent key={id} connectionId={id} />
}

export function ConnectionDetailPage() {
  return (
    <AdminGate>
      <ConnectionDetailRoute />
    </AdminGate>
  )
}
