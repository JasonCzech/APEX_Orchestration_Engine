/**
 * /admin/connections — the adapter connection registry as a card grid grouped
 * by port kind (plan Part 2 route table + UX 2.f, D7). Admin-gated.
 *
 * Create-time 422s carry the registered-provider list in the problem detail
 * ("unknown provider 'x' for kind 'y'; registered providers: a, b") — the
 * panel surfaces that message verbatim as an inline error, never a toast.
 */
import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router'

import {
  PORT_KINDS,
  useConnectionsIndex,
  useCreateConnection,
  useSetConnectionEnabled,
  type Connection,
  type PortKind,
} from '@/api/hooks/useConnections'
import { ProblemCard } from '@/components/ProblemCard'
import { formatRelative } from '@/utils/time'

import { groupConnectionsByKind, kindLabel, parseJsonObject } from './adminLogic'
import { AdminGate, TogglePill } from './adminShared'
import './admin.css'

const SKELETON_CARDS = 4

function ConnectionCard({ connection }: { connection: Connection }) {
  const navigate = useNavigate()
  const setEnabled = useSetConnectionEnabled()

  return (
    <div
      className="adm-card glass-panel"
      data-testid={`conn-card-${connection.id}`}
      onClick={() => void navigate(`/admin/connections/${connection.id}`)}
    >
      <div className="adm-card-top">
        <span className="adm-card-name">{connection.name}</span>
        <TogglePill
          enabled={connection.enabled}
          label={`Toggle ${connection.name}`}
          pending={setEnabled.isPending}
          onToggle={() =>
            setEnabled.mutate({ connectionId: connection.id, enabled: !connection.enabled })
          }
        />
      </div>
      <div className="adm-card-chips">
        <span className="dash-context-chip">{connection.provider}</span>
        <span className="dash-context-chip adm-chip-muted">
          {connection.project_id ?? 'global'}
        </span>
      </div>
      <span className="adm-card-time" title={connection.updated_at}>
        updated {formatRelative(connection.updated_at)}
      </span>
    </div>
  )
}

/** Inline create panel — kind, provider, name, project, base_url, options, secret_ref. */
function CreateConnectionPanel({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate()
  const create = useCreateConnection()
  const [kind, setKind] = useState<PortKind>('work_tracking')
  const [provider, setProvider] = useState('')
  const [name, setName] = useState('')
  const [project, setProject] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [optionsText, setOptionsText] = useState('{}')
  const [secretRef, setSecretRef] = useState('')

  const optionsParse = parseJsonObject(optionsText)
  const canSubmit =
    provider.trim() !== '' && name.trim() !== '' && optionsParse.ok && !create.isPending

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSubmit || !optionsParse.ok) return
    create.mutate(
      {
        kind,
        provider: provider.trim(),
        name: name.trim(),
        project_id: project.trim() || null,
        base_url: baseUrl.trim() || null,
        options: optionsParse.value,
        secret_ref: secretRef.trim() || null,
      },
      { onSuccess: (created) => void navigate(`/admin/connections/${created.id}`) },
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
            onChange={(event) => setKind(event.target.value as PortKind)}
          >
            {PORT_KINDS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
        <label className="adm-field">
          <span className="adm-field-label">Provider</span>
          <input
            type="text"
            className="field-input"
            aria-label="Provider"
            placeholder="jira"
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
            placeholder="jira-prod"
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
            placeholder="https://jira.example.com"
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
          rows={4}
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
  const connections = useConnectionsIndex()
  const [creating, setCreating] = useState(false)
  const groups = groupConnectionsByKind(connections.data ?? [])

  return (
    <section className="adm-page animate-enter">
      <header className="adm-toolbar glass-panel">
        {/* The topbar already renders the route-handle h1; this is a section label. */}
        <h2 className="adm-page-title">Connection registry</h2>
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

      {creating && <CreateConnectionPanel onClose={() => setCreating(false)} />}

      {connections.isPending ? (
        <div
          className="adm-skeleton"
          role="status"
          aria-busy="true"
          aria-label="Loading connections"
        >
          {Array.from({ length: SKELETON_CARDS }, (_, i) => (
            <div key={i} className="glass-panel adm-skeleton-card" />
          ))}
        </div>
      ) : connections.isError ? (
        <ProblemCard
          title="Connections unavailable"
          message={connections.error.message}
          onRetry={() => void connections.refetch()}
        />
      ) : groups.length === 0 ? (
        <div className="dash-empty">
          <h2>No connections yet</h2>
          <p className="dash-empty-hint">
            Connections bind port kinds (work tracking, logs, engines…) to provider adapters.
          </p>
        </div>
      ) : (
        groups.map((group) => (
          <section
            key={group.kind}
            className="adm-group"
            aria-label={`Kind ${kindLabel(group.kind)}`}
          >
            <header className="adm-group-header">
              <h2 className="adm-group-title">{kindLabel(group.kind)}</h2>
              <span className="adm-group-count">{group.connections.length}</span>
            </header>
            <div className="adm-card-grid">
              {group.connections.map((connection) => (
                <ConnectionCard key={connection.id} connection={connection} />
              ))}
            </div>
          </section>
        ))
      )}
    </section>
  )
}

export function ConnectionsPage() {
  return (
    <AdminGate>
      <ConnectionsContent />
    </AdminGate>
  )
}
