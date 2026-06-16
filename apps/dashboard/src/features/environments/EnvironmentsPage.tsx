/**
 * /environments — environment references grouped by application (plan Part 2
 * route table). Read path is two cached catalog indexes joined client-side;
 * mutations (create / delete) are operator+ and hidden from viewers — the
 * server enforces regardless.
 *
 * The list stays lean by design: last-scan info lives on the detail page only
 * (the list payload does not reliably carry snapshot summaries).
 */
import { useMemo, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router'

import {
  useApplicationsIndex,
  useCreateEnvironment,
  useDeleteEnvironment,
  useEnvironmentsIndex,
  type Application,
  type Environment,
} from '@/api/hooks/useEnvironments'
import { useConsumer } from '@/auth/AuthProvider'
import { roleAtLeast } from '@/auth/RequireRole'
import { ProblemCard } from '@/components/ProblemCard'
import { OverflowMenu } from '@/features/runs/PreflightModal'
import { formatRelative } from '@/utils/time'

import { HostsEditor, KindChip } from './environmentsForm'
import {
  KIND_OPTIONS,
  groupEnvironments,
  hostsToPayload,
  parseOptionsJson,
  type HostDraft,
} from './environmentsLogic'
import './environments.css'

const EM_DASH = '—'
const SKELETON_ROWS = 4

function EnvironmentRow({
  environment,
  canMutate,
  onDelete,
}: {
  environment: Environment
  canMutate: boolean
  onDelete: (environment: Environment) => void
}) {
  const navigate = useNavigate()
  const path = `/environments/${environment.id}`
  const open = () => void navigate(path)

  return (
    <tr className="env-row" onClick={open} data-testid={`env-row-${environment.id}`}>
      <td className="strong">{environment.name}</td>
      <td>
        <KindChip kind={environment.kind} />
      </td>
      <td>
        {environment.base_url ? (
          <span className="env-base-url" title={environment.base_url}>
            {environment.base_url}
          </span>
        ) : (
          <span className="env-muted">{EM_DASH}</span>
        )}
      </td>
      <td className="num">{environment.hosts.length}</td>
      <td className="env-time" title={environment.updated_at}>
        {formatRelative(environment.updated_at)}
      </td>
      <td className="env-actions-cell">
        <OverflowMenu
          label={`Environment actions: ${environment.name}`}
          items={[
            { label: 'Open', onSelect: open },
            ...(canMutate
              ? [
                  { label: 'Edit', onSelect: () => void navigate(`${path}?edit=1`) },
                  { label: 'Delete…', onSelect: () => onDelete(environment) },
                ]
              : []),
          ]}
        />
      </td>
    </tr>
  )
}

/** Inline create panel — application select, name, kind, base_url, hosts, options JSON. */
function CreateEnvironmentPanel({
  applications,
  onClose,
}: {
  applications: Application[]
  onClose: () => void
}) {
  const navigate = useNavigate()
  const create = useCreateEnvironment()
  const [applicationId, setApplicationId] = useState('')
  const [name, setName] = useState('')
  const [kind, setKind] = useState<string>(KIND_OPTIONS[0])
  const [baseUrl, setBaseUrl] = useState('')
  const [hosts, setHosts] = useState<HostDraft[]>([])
  const [optionsText, setOptionsText] = useState('{}')

  const optionsParse = parseOptionsJson(optionsText)
  const canSubmit =
    applicationId !== '' && name.trim() !== '' && optionsParse.ok && !create.isPending

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSubmit || !optionsParse.ok) return
    create.mutate(
      {
        application_id: applicationId,
        name: name.trim(),
        kind,
        base_url: baseUrl.trim() || null,
        hosts: hostsToPayload(hosts),
        options: optionsParse.value,
      },
      {
        onSuccess: (created) => void navigate(`/environments/${created.id}`),
      },
    )
  }

  return (
    <form className="glass-panel env-create-panel" onSubmit={submit} aria-label="New environment">
      <h2 className="env-panel-title">New environment</h2>
      <div className="env-form-grid">
        <label className="env-field">
          <span className="env-field-label">Application</span>
          <select
            className="field-select"
            aria-label="Application"
            value={applicationId}
            onChange={(event) => setApplicationId(event.target.value)}
          >
            <option value="">Select an application…</option>
            {applications.map((app) => (
              <option key={app.id} value={app.id}>
                {app.name} ({app.project_id})
              </option>
            ))}
          </select>
        </label>
        <label className="env-field">
          <span className="env-field-label">Name</span>
          <input
            type="text"
            className="field-input"
            aria-label="Name"
            placeholder="staging"
            value={name}
            onChange={(event) => setName(event.target.value)}
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
            {KIND_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
        <label className="env-field">
          <span className="env-field-label">Base URL</span>
          <input
            type="text"
            className="field-input"
            aria-label="Base URL"
            placeholder="https://staging.example.com"
            value={baseUrl}
            onChange={(event) => setBaseUrl(event.target.value)}
          />
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
          rows={4}
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
      {create.isError && (
        <div className="env-inline-error" role="alert">
          <span>Create failed: {create.error.message}</span>
        </div>
      )}

      <div className="env-panel-actions">
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={onClose}
          disabled={create.isPending}
        >
          Cancel
        </button>
        <button type="submit" className="btn btn-primary btn-sm" disabled={!canSubmit}>
          {create.isPending ? 'Creating…' : 'Create environment'}
        </button>
      </div>
    </form>
  )
}

/** Type-to-confirm delete modal (operator+). */
function DeleteEnvironmentModal({
  environment,
  onClose,
}: {
  environment: Environment
  onClose: () => void
}) {
  const remove = useDeleteEnvironment()
  const [confirmation, setConfirmation] = useState('')
  const canDelete = confirmation === environment.name && !remove.isPending

  function close() {
    if (remove.isPending) return
    onClose()
  }

  return (
    <div
      className="env-overlay"
      onClick={(event) => {
        if (event.target === event.currentTarget) close()
      }}
      onKeyDown={(event) => {
        if (event.key === 'Escape') close()
      }}
    >
      <div
        className="env-modal glass-panel"
        role="dialog"
        aria-modal="true"
        aria-label={`Delete environment ${environment.name}`}
      >
        <h2 className="env-panel-title">Delete environment</h2>
        <p className="env-modal-caption">
          This permanently removes <strong>{environment.name}</strong> and its scan history. Type
          the environment name to confirm.
        </p>
        <input
          type="text"
          className="field-input"
          aria-label="Type the environment name to confirm"
          placeholder={environment.name}
          value={confirmation}
          onChange={(event) => setConfirmation(event.target.value)}
        />
        {remove.isError && (
          <div className="env-inline-error" role="alert">
            <span>Delete failed: {remove.error.message}</span>
          </div>
        )}
        <div className="env-panel-actions">
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
            onClick={() => remove.mutate(environment.id, { onSuccess: onClose })}
          >
            {remove.isPending ? 'Deleting…' : 'Delete environment'}
          </button>
        </div>
      </div>
    </div>
  )
}

export function EnvironmentsPage() {
  const applications = useApplicationsIndex()
  const environments = useEnvironmentsIndex()
  const consumer = useConsumer()
  const canMutate = consumer ? roleAtLeast(consumer.role, 'operator') : false

  const [creating, setCreating] = useState(false)
  const [deleting, setDeleting] = useState<Environment | null>(null)

  const groups = useMemo(
    () => groupEnvironments(applications.data ?? [], environments.data ?? []),
    [applications.data, environments.data],
  )

  const isPending = applications.isPending || environments.isPending
  const queryError = environments.error ?? applications.error

  return (
    <section className="env-page animate-enter">
      <header className="env-toolbar glass-panel">
        {/* The topbar already renders the route-handle h1; this is a section label. */}
        <h2 className="env-page-title">Environment configurations</h2>
        {canMutate && !creating && (
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={() => setCreating(true)}
          >
            New environment
          </button>
        )}
      </header>

      {creating && (
        <CreateEnvironmentPanel
          applications={applications.data ?? []}
          onClose={() => setCreating(false)}
        />
      )}

      {isPending ? (
        <div
          className="env-skeleton"
          role="status"
          aria-busy="true"
          aria-label="Loading environments"
        >
          {Array.from({ length: SKELETON_ROWS }, (_, i) => (
            <div key={i} className="glass-panel env-skeleton-row" />
          ))}
        </div>
      ) : queryError ? (
        <ProblemCard
          title="Environments unavailable"
          message={queryError.message}
          onRetry={() => {
            void applications.refetch()
            void environments.refetch()
          }}
        />
      ) : groups.length === 0 ? (
        <div className="dash-empty">
          <h2>No environment configurations yet</h2>
          <p className="dash-empty-hint">
            Environment references point pipeline phases at the systems under test.
          </p>
        </div>
      ) : (
        groups.map((group) => (
          <section key={group.key} className="env-group" aria-label={`Application ${group.label}`}>
            <header className="env-group-header">
              <h2 className="env-group-title">{group.label}</h2>
              {group.project && <span className="env-group-project">{group.project}</span>}
            </header>
            <div className="data-table-wrap">
              <table className="data-table striped env-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Kind</th>
                    <th>Base URL</th>
                    <th className="num">Hosts</th>
                    <th>Updated</th>
                    <th className="env-actions-cell">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {group.environments.map((environment) => (
                    <EnvironmentRow
                      key={environment.id}
                      environment={environment}
                      canMutate={canMutate}
                      onDelete={setDeleting}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        ))
      )}

      {deleting && (
        <DeleteEnvironmentModal environment={deleting} onClose={() => setDeleting(null)} />
      )}
    </section>
  )
}
