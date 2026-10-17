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
import { Link, useNavigate, useSearchParams } from 'react-router'

import {
  environmentWriteMutationKey,
  useApplicationsIndex,
  useCreateApplication,
  useCreateEnvironment,
  useDeleteEnvironment,
  useEnvironmentsIndex,
  type Application,
  type Environment,
} from '@/api/hooks/useEnvironments'
import { usePendingMutationCount } from '@/api/hooks/usePendingMutationCount'
import { useConsumer } from '@/auth/AuthProvider'
import { canLaunchPipelines, canMutateAudience, isGlobalAdmin } from '@/auth/RequireRole'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { Dialog } from '@/components/Dialog'
import { ProblemCard } from '@/components/ProblemCard'
import { OverflowMenu } from '@/components/ux/OverflowMenu'
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

function CreateApplicationPanel({
  projectOptions,
  onClose,
  onCreated,
}: {
  projectOptions: string[] | null
  onClose: () => void
  onCreated: (application: Application) => void
}) {
  const create = useCreateApplication()
  const [projectId, setProjectId] = useState(projectOptions?.length === 1 ? projectOptions[0]! : '')
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const canSubmit = projectId.trim() !== '' && name.trim() !== '' && !create.isPending

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSubmit) return
    create.mutate(
      {
        project_id: projectId.trim(),
        name: name.trim(),
        description: description.trim() || null,
      },
      { onSuccess: onCreated },
    )
  }

  return (
    <form className="glass-panel env-create-panel" onSubmit={submit} aria-label="New application">
      <div>
        <h2 className="env-panel-title">New application</h2>
        <p className="env-panel-caption">Applications group the environments used by tests.</p>
      </div>
      <div className="env-form-grid">
        <label className="env-field">
          <span className="env-field-label">Project</span>
          {projectOptions ? (
            <select
              className="field-select"
              aria-label="Project"
              value={projectId}
              onChange={(event) => setProjectId(event.target.value)}
            >
              <option value="">Select a project…</option>
              {projectOptions.map((project) => <option key={project} value={project}>{project}</option>)}
            </select>
          ) : (
            <input
              type="text"
              className="field-input"
              aria-label="Project"
              placeholder="project-id"
              value={projectId}
              onChange={(event) => setProjectId(event.target.value)}
            />
          )}
        </label>
        <label className="env-field">
          <span className="env-field-label">Name</span>
          <input
            type="text"
            className="field-input"
            aria-label="Application name"
            placeholder="Checkout"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
      </div>
      <label className="env-field">
        <span className="env-field-label">Description</span>
        <textarea
          className="field-input"
          aria-label="Application description"
          rows={3}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
        />
      </label>
      {create.isError && <p className="env-form-error" role="alert">Create failed: {create.error.message}</p>}
      <div className="env-panel-actions">
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClose} disabled={create.isPending}>Cancel</button>
        <button type="submit" className="btn btn-primary btn-sm" disabled={!canSubmit}>
          {create.isPending ? 'Creating…' : 'Create application'}
        </button>
      </div>
    </form>
  )
}

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
  const writeCount = usePendingMutationCount(environmentWriteMutationKey(environment.id))
  const writePending = writeCount > 0
  const path = `/environments/${environment.id}`
  const open = () => void navigate(path)

  return (
    <tr className="env-row" data-testid={`env-row-${environment.id}`}>
      <td className="strong">
        <Link className="env-name-link" to={path}>
          {environment.name}
        </Link>
      </td>
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
                  {
                    label: 'Edit',
                    onSelect: () => void navigate(`${path}?edit=1`),
                    disabled: writePending,
                  },
                  {
                    label: 'Delete…',
                    onSelect: () => onDelete(environment),
                    disabled: writePending,
                  },
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
  initialApplicationId = '',
  canApproveTarget,
  onClose,
}: {
  applications: Application[]
  initialApplicationId?: string
  canApproveTarget: boolean
  onClose: () => void
}) {
  const navigate = useNavigate()
  const create = useCreateEnvironment()
  const [applicationId, setApplicationId] = useState(initialApplicationId)
  const [name, setName] = useState('')
  const [kind, setKind] = useState<string>(KIND_OPTIONS[0])
  const [baseUrl, setBaseUrl] = useState('')
  const [hosts, setHosts] = useState<HostDraft[]>([])
  const [optionsText, setOptionsText] = useState('{}')

  const optionsParse = parseOptionsJson(optionsText)
  const hasReservedOptions =
    optionsParse.ok && Object.keys(optionsParse.value).some((key) => key.startsWith('_apex_'))
  const canSubmit =
    applicationId !== '' &&
    name.trim() !== '' &&
    optionsParse.ok &&
    (canApproveTarget || !hasReservedOptions) &&
    !create.isPending

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
      {applications.length === 0 && (
        <p className="env-form-error" role="alert">
          Create an application before adding an environment.
        </p>
      )}
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
        {canApproveTarget && <label className="env-field">
          <span className="env-field-label">Base URL</span>
          <input
            type="text"
            className="field-input"
            aria-label="Base URL"
            placeholder="https://staging.example.com"
            value={baseUrl}
            onChange={(event) => setBaseUrl(event.target.value)}
          />
        </label>}
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
      {!canApproveTarget && hasReservedOptions && (
        <p className="env-form-error" role="alert">
          Options beginning with _apex_ require a global administrator.
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
  const remove = useDeleteEnvironment(environment.id)
  const writeCount = usePendingMutationCount(environmentWriteMutationKey(environment.id))
  const [confirmation, setConfirmation] = useState('')
  const canDelete = confirmation === environment.name && writeCount === 0

  function close() {
    if (remove.isPending) return
    onClose()
  }

  return (
    <Dialog
      overlayClassName="env-overlay"
      className="env-modal glass-panel"
      ariaLabel={`Delete environment ${environment.name}`}
      onClose={close}
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
    </Dialog>
  )
}

export function EnvironmentsPage() {
  const applications = useApplicationsIndex()
  const environments = useEnvironmentsIndex()
  const consumer = useConsumer()
  const canMutate = canLaunchPipelines(consumer)
  const canApproveTarget = isGlobalAdmin(consumer)
  const projectWideScopes = Array.from(new Set(
    (consumer?.scopes ?? [])
      .filter((scope) => !scope.app_id)
      .map((scope) => scope.project_id),
  )).sort()
  const canCreateApplication = canMutate && (isGlobalAdmin(consumer) || projectWideScopes.length > 0)
  const applicationProjectOptions = (consumer?.scopes.length ?? 0) > 0 ? projectWideScopes : null

  const [searchParams] = useSearchParams()
  const applicationFilter = searchParams.get('application')
  const [search, setSearch] = useState('')
  const [projectFilter, setProjectFilter] = useState('')
  const [creatingApplication, setCreatingApplication] = useState(false)
  const [creatingEnvironmentFor, setCreatingEnvironmentFor] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<Environment | null>(null)

  const groups = useMemo(
    () => groupEnvironments(applications.data ?? [], environments.data ?? []).filter((group) =>
      (!applicationFilter || group.key === applicationFilter) &&
      (!projectFilter || group.project === projectFilter) &&
      (!search.trim() || `${group.label} ${group.project ?? ''} ${group.environments.map((environment) => environment.name).join(' ')}`.toLowerCase().includes(search.trim().toLowerCase())),
    ),
    [applications.data, environments.data, applicationFilter, projectFilter, search],
  )

  const isPending = applications.isPending || environments.isPending
  const queryError = environments.error ?? applications.error
  const hasCachedData = Boolean(applications.data && environments.data)

  return (
    <section className="env-page animate-enter">
      <header className="env-toolbar glass-panel">
        {/* The topbar already renders the route-handle h1; this is a section label. */}
        <div>
          <h2 className="env-page-title">Applications &amp; environments</h2>
          <p className="env-toolbar-caption">Define applications first, then add their test environments.</p>
        </div>
        <div className="env-toolbar-actions">
          {canCreateApplication && !creatingApplication && creatingEnvironmentFor === null && (
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => setCreatingApplication(true)}>
              New application
            </button>
          )}
        </div>
      </header>

      {applicationFilter && <Link className="btn btn-ghost" to="/environments">All applications</Link>}
      <details className="ux-filters" open><summary>Filters</summary><div><label className="ux-field">Search applications and environments<input className="field-input" value={search} onChange={(event) => setSearch(event.target.value)} /></label><label className="ux-field">Project<select className="field-select" value={projectFilter} onChange={(event) => setProjectFilter(event.target.value)}><option value="">All projects</option>{[...new Set((applications.data ?? []).map((app) => app.project_id))].sort().map((project) => <option key={project}>{project}</option>)}</select></label></div></details>
      {creatingApplication && (
        <CreateApplicationPanel
          projectOptions={applicationProjectOptions}
          onClose={() => setCreatingApplication(false)}
          onCreated={(application) => {
            setCreatingApplication(false)
            setCreatingEnvironmentFor(application.id)
          }}
        />
      )}

      {creatingEnvironmentFor !== null && (
        <CreateEnvironmentPanel
          applications={applications.data ?? []}
          initialApplicationId={creatingEnvironmentFor}
          canApproveTarget={canApproveTarget}
          onClose={() => setCreatingEnvironmentFor(null)}
        />
      )}

      {queryError && hasCachedData && (
        <CachedDataWarning
          error={queryError}
          onRetry={() => {
            void applications.refetch()
            void environments.refetch()
          }}
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
      ) : queryError && !hasCachedData ? (
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
          <h2>{search || projectFilter || applicationFilter ? 'No matching applications' : 'No applications yet'}</h2>
          <p className="dash-empty-hint">
            {canCreateApplication
              ? 'Create an application first, then add the environments used by tests.'
              : consumer?.role !== 'viewer'
                ? 'Application creation requires project-wide access.'
                : 'Your role has read-only access. Ask a project operator to create an application.'}
          </p>
          {canCreateApplication && (
            <button type="button" className="btn btn-primary" onClick={() => setCreatingApplication(true)}>
              Create application
            </button>
          )}
        </div>
      ) : (
        groups.map((group) => (
          <section key={group.key} className="env-group" aria-label={`Application ${group.label}`}>
            <header className="env-group-header">
              <div className="env-group-heading">
                <h2 className="env-group-title"><Link to={`/environments?application=${encodeURIComponent(group.key)}`}>{group.label}</Link></h2>
                {group.project && <span className="env-group-project">{group.project}</span>}
              </div>
              {canMutateAudience(consumer, group.project, group.key) && !creatingApplication && creatingEnvironmentFor === null && (
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => setCreatingEnvironmentFor(group.key)}>
                  Add environment
                </button>
              )}
            </header>
            {group.environments.length === 0 ? (
              <div className="glass-panel env-group-empty">
                <span>No environments configured.</span>
                {canMutateAudience(consumer, group.project, group.key) && !creatingApplication && creatingEnvironmentFor === null && (
                  <button type="button" className="btn btn-secondary btn-sm" onClick={() => setCreatingEnvironmentFor(group.key)}>
                    Add first environment
                  </button>
                )}
              </div>
            ) : <div className="data-table-wrap">
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
                      canMutate={canMutateAudience(consumer, group.project, group.key)}
                      onDelete={setDeleting}
                    />
                  ))}
                </tbody>
              </table>
            </div>}
          </section>
        ))
      )}

      {!isPending && !queryError && !canMutate && groups.length > 0 && (
        <p className="env-permission-note">Your role has read-only access to applications and environments.</p>
      )}

      {deleting && (
        <DeleteEnvironmentModal environment={deleting} onClose={() => setDeleting(null)} />
      )}
    </section>
  )
}
