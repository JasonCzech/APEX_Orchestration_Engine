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
import { useState, type FormEvent } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'

import {
  useApplicationsIndex,
  useEnvironment,
  useUpdateEnvironment,
  type Environment,
} from '@/api/hooks/useEnvironments'
import {
  useEnvironmentInventory,
  useRescanEnvironment,
  type SnapshotView,
} from '@/api/hooks/useInventory'
import { useConsumer } from '@/auth/AuthProvider'
import { roleAtLeast } from '@/auth/RequireRole'
import { ProblemCard } from '@/components/ProblemCard'
import { JsonViewer } from '@/components/viewers/JsonViewer'
import { formatRelative } from '@/utils/time'

import { HostsEditor, KindChip } from './environmentsForm'
import {
  KIND_OPTIONS,
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
      <h2 className="env-card-title">Reference</h2>
      <dl className="env-ref-grid">
        <dt>Base URL</dt>
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
      <h3 className="env-card-subtitle">Options</h3>
      <JsonViewer value={environment.options} ariaLabel="Environment options" />
    </div>
  )
}

/** Inline edit form for base_url / kind / hosts / options (PATCH; operator+). */
function EditEnvironmentForm({
  environment,
  onDone,
}: {
  environment: Environment
  onDone: () => void
}) {
  const update = useUpdateEnvironment()
  const [baseUrl, setBaseUrl] = useState(environment.base_url ?? '')
  const [kind, setKind] = useState(environment.kind ?? KIND_OPTIONS[0])
  const [hosts, setHosts] = useState<HostDraft[]>(() => hostsToDrafts(environment.hosts))
  const [optionsText, setOptionsText] = useState(() =>
    JSON.stringify(environment.options, null, 2),
  )

  const optionsParse = parseOptionsJson(optionsText)
  const canSave = optionsParse.ok && !update.isPending

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSave || !optionsParse.ok) return
    update.mutate(
      {
        environmentId: environment.id,
        body: {
          base_url: baseUrl.trim() || null,
          kind,
          hosts: hostsToPayload(hosts),
          options: optionsParse.value,
        },
      },
      { onSuccess: onDone },
    )
  }

  return (
    <form className="env-card glass-panel" onSubmit={submit} aria-label="Edit environment">
      <h2 className="env-card-title">Edit reference</h2>
      <div className="env-form-grid">
        <label className="env-field">
          <span className="env-field-label">Base URL</span>
          <input
            type="text"
            className="field-input"
            aria-label="Base URL"
            value={baseUrl}
            onChange={(event) => setBaseUrl(event.target.value)}
          />
        </label>
        <label className="env-field">
          <span className="env-field-label">Kind</span>
          <select
            className="field-select"
            aria-label="Kind"
            value={kind}
            onChange={(event) => setKind(event.target.value)}
          >
            {/* Preserve a non-standard kind already on the record. */}
            {!KIND_OPTIONS.includes(kind as (typeof KIND_OPTIONS)[number]) && (
              <option value={kind}>{kind}</option>
            )}
            {KIND_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="env-field">
        <span className="env-field-label">Hosts</span>
        <HostsEditor hosts={hosts} onChange={setHosts} />
      </div>
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
      </label>
      {!optionsParse.ok && (
        <p className="env-form-error" role="alert">
          {optionsParse.message}
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
          onClick={onDone}
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
}: {
  environmentId: string
  canMutate: boolean
  rescan: ReturnType<typeof useRescanEnvironment>
}) {
  const inventory = useEnvironmentInventory(environmentId)

  return (
    <div className="env-card glass-panel">
      <header className="env-card-header">
        <h2 className="env-card-title">Inventory</h2>
        {inventory.data?.snapshot && (
          <span className="env-scan-caption">
            Scanned {formatRelative(inventory.data.snapshot.scanned_at)}
            {inventory.data.snapshot.stale && (
              <span className="status-badge warning env-stale-chip">stale</span>
            )}
          </span>
        )}
      </header>

      {rescan.isError && (
        <div className="env-inline-error" role="alert">
          <span>{rescan.error.message}</span>
          {canMutate && (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => rescan.mutate(environmentId)}
              disabled={rescan.isPending}
            >
              Retry
            </button>
          )}
        </div>
      )}

      {inventory.isPending ? (
        <div role="status" aria-label="Loading inventory" className="env-muted">
          Loading inventory…
        </div>
      ) : inventory.isError ? (
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
        <div className="dash-empty small" data-testid="env-inventory-empty">
          <h3>Never scanned</h3>
          <p className="dash-empty-hint">
            Run a scan to capture the services deployed in this environment.
          </p>
          {canMutate && (
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => rescan.mutate(environmentId)}
              disabled={rescan.isPending}
            >
              {rescan.isPending ? 'Scanning…' : 'Rescan'}
            </button>
          )}
        </div>
      ) : (
        <ServicesTable snapshot={inventory.data.snapshot} />
      )}
    </div>
  )
}

export function EnvironmentDetailPage() {
  const { id = '' } = useParams<{ id: string }>()
  const [searchParams] = useSearchParams()
  const environment = useEnvironment(id)
  const applications = useApplicationsIndex()
  const rescan = useRescanEnvironment()
  const consumer = useConsumer()
  const canMutate = consumer ? roleAtLeast(consumer.role, 'operator') : false
  // ?edit=1 lets the list's row menu land directly in edit mode.
  const [editing, setEditing] = useState(searchParams.get('edit') === '1')

  if (environment.isPending) {
    return (
      <div
        className="env-skeleton animate-enter"
        role="status"
        aria-busy="true"
        aria-label="Loading environment"
      >
        <div className="glass-panel env-skeleton-row" />
        <div className="glass-panel env-skeleton-card" />
      </div>
    )
  }

  if (environment.isError) {
    return (
      <ProblemCard
        title="Environment unavailable"
        message={environment.error.message}
        onRetry={() => void environment.refetch()}
      />
    )
  }

  const env = environment.data
  const app = applications.data?.find((candidate) => candidate.id === env.application_id)

  return (
    <section className="env-page animate-enter">
      <header className="env-detail-header glass-panel">
        <div className="env-detail-heading">
          <nav className="env-breadcrumb" aria-label="Breadcrumb">
            <Link to="/environments">Environment Configurations</Link>
            {app && (
              <>
                <span className="env-breadcrumb-sep">/</span>
                <span>{app.project_id}</span>
                <span className="env-breadcrumb-sep">/</span>
                <span>{app.name}</span>
              </>
            )}
          </nav>
          <div className="env-detail-title-row">
            {/* The topbar already renders the route-handle h1. */}
            <h2 className="env-detail-title">{env.name}</h2>
            <KindChip kind={env.kind} />
          </div>
        </div>
        {canMutate && (
          <div className="env-detail-actions">
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              aria-pressed={editing}
              onClick={() => setEditing((value) => !value)}
            >
              {editing ? 'Close editor' : 'Edit'}
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() => rescan.mutate(env.id)}
              disabled={rescan.isPending}
            >
              {rescan.isPending ? 'Scanning…' : 'Rescan'}
            </button>
          </div>
        )}
      </header>

      <div className="env-detail-panels">
        {editing ? (
          <EditEnvironmentForm environment={env} onDone={() => setEditing(false)} />
        ) : (
          <ReferenceCard environment={env} />
        )}
        <InventoryPanel environmentId={env.id} canMutate={canMutate} rescan={rescan} />
      </div>
    </section>
  )
}
