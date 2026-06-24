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
import { useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'

import {
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
import { Dialog } from '@/components/Dialog'
import { ProblemCard } from '@/components/ProblemCard'

import { kindLabel, parseJsonObject } from './adminLogic'
import { AdminGate, TogglePill } from './adminShared'
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
  const update = useUpdateConnection()
  const [name, setName] = useState(connection.name)
  const [provider, setProvider] = useState(connection.provider)
  const [project, setProject] = useState(connection.project_id ?? '')
  const [baseUrl, setBaseUrl] = useState(connection.base_url ?? '')
  const [secretRef, setSecretRef] = useState(connection.secret_ref ?? '')
  const [optionsText, setOptionsText] = useState(() =>
    JSON.stringify(connection.options, null, 2),
  )

  const optionsParse = parseJsonObject(optionsText)
  const canSave =
    name.trim() !== '' && provider.trim() !== '' && optionsParse.ok && !update.isPending

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSave || !optionsParse.ok) return
    update.mutate({
      connectionId: connection.id,
      body: {
        name: name.trim(),
        provider: provider.trim(),
        project_id: project.trim() || null,
        base_url: baseUrl.trim() || null,
        secret_ref: secretRef.trim() || null,
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
            onChange={(event) => setProvider(event.target.value)}
          />
          <span className="adm-field-help">must be a registered provider</span>
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
          <input
            type="text"
            className="field-input"
            aria-label="Project"
            placeholder="leave empty for global"
            value={project}
            onChange={(event) => setProject(event.target.value)}
          />
        </label>
        <label className="adm-field">
          <span className="adm-field-label">Base URL (optional)</span>
          <input
            type="text"
            className="field-input"
            aria-label="Base URL"
            value={baseUrl}
            onChange={(event) => setBaseUrl(event.target.value)}
          />
        </label>
        <label className="adm-field">
          <span className="adm-field-label">Secret ref</span>
          <input
            type="text"
            className="field-input"
            aria-label="Secret ref"
            placeholder="env:JIRA_API_TOKEN"
            value={secretRef}
            onChange={(event) => setSecretRef(event.target.value)}
          />
          <span className="adm-field-help">env:NAME — references only, never raw secrets</span>
        </label>
      </div>
      <label className="adm-field">
        <span className="adm-field-label">Options (JSON)</span>
        <textarea
          className="field-input adm-json-input"
          aria-label="Options JSON"
          rows={6}
          spellCheck={false}
          value={optionsText}
          onChange={(event) => setOptionsText(event.target.value)}
        />
      </label>
      {!optionsParse.ok && (
        <p className="adm-form-error" role="alert">
          {optionsParse.message}
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
  const put = usePutHostMappings()
  const [rows, setRows] = useState<MappingDraft[]>(() =>
    initial.map(({ pattern, target, enabled }) => ({ pattern, target, enabled })),
  )

  function patchRow(index: number, patch: Partial<MappingDraft>) {
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
                      onChange={(event) => patchRow(index, { pattern: event.target.value })}
                    />
                  </td>
                  <td>
                    <input
                      type="text"
                      className="field-input"
                      aria-label={`Mapping ${index + 1} target`}
                      value={row.target}
                      onChange={(event) => patchRow(index, { target: event.target.value })}
                    />
                  </td>
                  <td className="adm-mapping-enabled">
                    <input
                      type="checkbox"
                      aria-label={`Mapping ${index + 1} enabled`}
                      checked={row.enabled}
                      onChange={(event) => patchRow(index, { enabled: event.target.checked })}
                    />
                  </td>
                  <td className="adm-mapping-remove">
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      aria-label={`Remove mapping ${index + 1}`}
                      onClick={() => setRows((current) => current.filter((_, i) => i !== index))}
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
          onClick={() =>
            setRows((current) => [...current, { pattern: '', target: '', enabled: true }])
          }
        >
          Add mapping
        </button>
        <button
          type="button"
          className="btn btn-primary btn-sm"
          disabled={!canSave || put.isPending}
          onClick={() =>
            put.mutate({
              connectionId,
              mappings: rows.map((row) => ({
                pattern: row.pattern.trim(),
                target: row.target.trim(),
                enabled: row.enabled,
              })),
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
  if (mappings.isError) {
    return (
      <ProblemCard
        title="Host mappings unavailable"
        message={mappings.error.message}
        onRetry={() => void mappings.refetch()}
      />
    )
  }
  // Key on the fetch timestamp so a refetch reseeds the draft rows.
  return (
    <HostMappingsEditor
      key={mappings.dataUpdatedAt}
      connectionId={connectionId}
      initial={mappings.data}
    />
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
  const remove = useDeleteConnection()
  const [confirmation, setConfirmation] = useState('')
  const canDelete = confirmation === connection.name && !remove.isPending

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
                onSuccess: () => void navigate('/admin/connections'),
              })
            }
          >
            {remove.isPending ? 'Deleting…' : 'Delete connection'}
          </button>
        </div>
    </Dialog>
  )
}

function ConnectionDetailContent() {
  const { id = '' } = useParams<{ id: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const connection = useConnection(id)
  const setEnabled = useSetConnectionEnabled()
  const probe = useTestConnection()
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

  if (connection.isError) {
    return (
      <ProblemCard
        title="Connection unavailable"
        message={connection.error.message}
        onRetry={() => void connection.refetch()}
      />
    )
  }

  const conn = connection.data

  return (
    <section className="adm-page animate-enter">
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
              pending={setEnabled.isPending}
              onToggle={() => setEnabled.mutate({ connectionId: conn.id, enabled: !conn.enabled })}
            />
          </div>
        </div>
        <div className="adm-detail-actions">
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => probe.mutate(conn.id)}
            disabled={probe.isPending}
          >
            {probe.isPending ? 'Testing…' : 'Test connection'}
          </button>
          <button
            type="button"
            className="btn btn-danger btn-sm"
            onClick={() => setDeleting(true)}
          >
            Delete
          </button>
        </div>
      </header>

      <ProbePanel probe={probe} />

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
        // Key on the record version so an outside update reseeds the form.
        <ConfigTab key={conn.updated_at} connection={conn} />
      ) : (
        <HostMappingsTab connectionId={conn.id} />
      )}

      {deleting && <DeleteConnectionModal connection={conn} onClose={() => setDeleting(false)} />}
    </section>
  )
}

export function ConnectionDetailPage() {
  return (
    <AdminGate>
      <ConnectionDetailContent />
    </AdminGate>
  )
}
