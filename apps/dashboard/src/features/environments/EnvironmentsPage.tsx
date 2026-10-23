import { useTimestampPreference } from '@/theme/preferences'
import { StateCard } from '@/components/ux/StateCard'
import { Skeleton } from '@/components/ux/Skeleton'
import { useProjectLabels } from '@/api/hooks/useProjectLabels'
import { ProjectIdentity } from '@/components/ux/ProjectIdentity'
/**
 * /environments — applications and their test environments, grouped by
 * project. Each row says whether external engines can run against the
 * environment (target status) and when it was last scanned. Filters live in the
 * URL (?q, ?project, ?target, ?archived, ?application). Create, edit and delete
 * are operator+ (application delete is admin); the server enforces both.
 */
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'

import {
  environmentWriteMutationKey,
  useApplicationsIndex,
  useCreateApplication,
  useCreateEnvironment,
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
import { TableToolbar } from '@/components/ux/TableToolbar'
import { OverflowMenu } from '@/components/ux/OverflowMenu'
import { formatRelative, formatTimestamp } from '@/utils/time'

import { HostsEditor } from './environmentsForm'
import {
  ArchiveApplicationDialog,
  DeleteApplicationDialog,
  DeleteEnvironmentDialog,
  EditApplicationDialog,
} from './environmentsDialogs'
import {
  KIND_LABELS,
  KIND_OPTIONS,
  TARGET_STATUS_LABELS,
  groupEnvironments,
  hostsToPayload,
  kindLabel,
  parseOptionsJson,
  projectGroups,
  targetHost,
  targetStatus,
  type EnvironmentGroup,
  type HostDraft,
  type ReadinessFilter,
} from './environmentsLogic'
import './environments.css'

const EM_DASH = '—'
const SKELETON_ROWS = 4
const COLLAPSED_STORAGE_KEY = 'apex.environments.collapsedProjects'

function readCollapsed(): string[] {
  try {
    const raw: unknown = JSON.parse(window.localStorage.getItem(COLLAPSED_STORAGE_KEY) ?? '[]')
    return Array.isArray(raw) ? raw.filter((item): item is string => typeof item === 'string') : []
  } catch {
    return []
  }
}

function writeCollapsed(projects: string[]) {
  try {
    window.localStorage.setItem(COLLAPSED_STORAGE_KEY, JSON.stringify(projects))
  } catch {
    // Remembering collapsed projects is a convenience only.
  }
}

/** Target status badge shared with the detail page's wording. */
export function TargetBadge({ environment }: { environment: Pick<Environment, 'base_url' | 'target_approved'> }) {
  const status = targetStatus(environment)
  const tone = status === 'approved' ? 'success' : status === 'needs-approval' ? 'warning' : 'neutral'
  return <span className={`status-badge ${tone} env-target-badge`}>{TARGET_STATUS_LABELS[status]}</span>
}

function CreateApplicationDialog({
  projectOptions,
  existing,
  onClose,
  onCreated,
}: {
  projectOptions: string[] | null
  existing: Application[]
  onClose: () => void
  onCreated: (application: Application) => void
}) {
  const create = useCreateApplication()
  const labels = useProjectLabels()
  const projectCaption = (id: string) => `${(!labels.isError && labels.data?.find(row => row.project_id === id)?.name) || 'Project name unavailable'} (${id})`
  const [projectId, setProjectId] = useState(projectOptions?.length === 1 ? projectOptions[0]! : '')
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  // Names are unique per project, archived applications included (exact match, like the database).
  const duplicateOf = name.trim() === '' ? undefined : existing.find(app => app.project_id === projectId.trim() && app.name === name.trim())
  const duplicate = Boolean(duplicateOf)
  const canSubmit = projectId.trim() !== '' && name.trim() !== '' && !duplicate && !create.isPending

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSubmit) return
    create.mutate(
      { project_id: projectId.trim(), name: name.trim(), description: description.trim() || null },
      { onSuccess: onCreated },
    )
  }

  return (
    <Dialog overlayClassName="env-overlay" className="env-modal env-modal--wide glass-panel" ariaLabel="New application"
      onClose={() => { if (!create.isPending) onClose() }} panelAs="form" onSubmit={submit}>
      <div>
        <h2 className="env-panel-title">New application</h2>
        <p className="env-panel-caption">An application is a system under test. Add its test environments next.</p>
      </div>
      <div className="env-form-grid">
        <label className="env-field">
          <span className="env-field-label">Project</span>
          {projectOptions ? (
            <select className="field-select" aria-label="Project" value={projectId} onChange={(event) => setProjectId(event.target.value)}>
              <option value="">Select a project…</option>
              {projectOptions.map((project) => <option key={project} value={project}>{projectCaption(project)}</option>)}
            </select>
          ) : (
            <input type="text" className="field-input" aria-label="Project" placeholder="project-id" value={projectId} onChange={(event) => setProjectId(event.target.value)} />
          )}
        </label>
        <label className="env-field">
          <span className="env-field-label">Name</span>
          <input type="text" className="field-input" aria-label="Application name" placeholder="Checkout" value={name}
            onChange={(event) => setName(event.target.value)} aria-invalid={duplicate || undefined} />
        </label>
      </div>
      {duplicate && <p className="env-form-error" role="alert">{duplicateOf?.archived_at
        ? 'An archived application in this project already uses this name. Unarchive it instead, or choose another name.'
        : 'This project already has an application with this name.'}</p>}
      <label className="env-field">
        <span className="env-field-label">Description</span>
        <textarea className="field-input" aria-label="Application description" rows={3} value={description} onChange={(event) => setDescription(event.target.value)} />
      </label>
      {create.isError && <p className="env-form-error" role="alert">Create failed: {create.error.message}</p>}
      <div className="env-panel-actions">
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClose} disabled={create.isPending}>Cancel</button>
        <button type="submit" className="btn btn-primary btn-sm" disabled={!canSubmit}>{create.isPending ? 'Creating…' : 'Create application'}</button>
      </div>
    </Dialog>
  )
}

/** New environment form, shown inside the application it is for. */
function CreateEnvironmentPanel({
  application,
  siblingNames,
  canApproveTarget,
  justCreated,
  onClose,
}: {
  application: Application
  siblingNames: string[]
  canApproveTarget: boolean
  justCreated: boolean
  onClose: () => void
}) {
  const navigate = useNavigate()
  const create = useCreateEnvironment()
  const [name, setName] = useState('')
  const [kind, setKind] = useState<string>(KIND_OPTIONS[0])
  const [baseUrl, setBaseUrl] = useState('')
  const [hosts, setHosts] = useState<HostDraft[]>([])
  const [optionsText, setOptionsText] = useState('{}')
  const formRef = useRef<HTMLFormElement>(null)
  const nameRef = useRef<HTMLInputElement>(null)

  // The form opens where it was asked for: bring it into view and focus it.
  useEffect(() => {
    formRef.current?.scrollIntoView?.({ block: 'nearest' })
    nameRef.current?.focus({ preventScroll: true })
  }, [])

  const optionsParse = parseOptionsJson(optionsText)
  const hasReservedOptions = optionsParse.ok && Object.keys(optionsParse.value).some((key) => key.startsWith('_apex_'))
  const duplicate = name.trim() !== '' && siblingNames.includes(name.trim())
  const canSubmit = name.trim() !== '' && !duplicate && optionsParse.ok && (canApproveTarget || !hasReservedOptions) && !create.isPending

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSubmit || !optionsParse.ok) return
    create.mutate(
      {
        application_id: application.id,
        name: name.trim(),
        kind,
        base_url: baseUrl.trim() || null,
        hosts: hostsToPayload(hosts),
        options: optionsParse.value,
      },
      { onSuccess: (created) => void navigate(`/environments/${created.id}`) },
    )
  }

  return (
    <form ref={formRef} className="glass-panel env-create-panel" onSubmit={submit} aria-label="New environment">
      <div>
        <h3 className="env-panel-title">New environment for {application.name}</h3>
        {justCreated && <p className="env-panel-caption" role="status">{application.name} was created. Add its first environment now, or skip and add one later.</p>}
      </div>
      <div className="env-form-grid">
        <label className="env-field">
          <span className="env-field-label">Name</span>
          <input ref={nameRef} type="text" className="field-input" aria-label="Name" placeholder="Staging" value={name}
            onChange={(event) => setName(event.target.value)} aria-invalid={duplicate || undefined} />
        </label>
        <label className="env-field">
          <span className="env-field-label">Platform</span>
          <select className="field-select" aria-label="Platform" value={kind} onChange={(event) => setKind(event.target.value)}>
            {KIND_OPTIONS.map((option) => <option key={option} value={option}>{KIND_LABELS[option]}</option>)}
          </select>
          <span className="env-field-help">How the environment is hosted. Inventory scans read Kubernetes services; other platforms are described by their hosts.</span>
        </label>
      </div>
      {duplicate && <p className="env-form-error" role="alert">{application.name} already has an environment with this name.</p>}
      {canApproveTarget ? (
        <label className="env-field">
          <span className="env-field-label">Execution target URL</span>
          <input type="text" className="field-input" aria-label="Execution target URL" placeholder="https://staging.example.com" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} />
          <span className="env-field-help">Where external engines (APEX Load, LoadRunner) send traffic. You approve it on the environment’s page.</span>
        </label>
      ) : (
        <p className="env-field-help env-target-note">A global administrator sets and approves the execution target URL. Until then, simulated tests can use this environment as context, but external engines can’t run against it.</p>
      )}
      <div className="env-field">
        <span className="env-field-label">Hosts</span>
        <HostsEditor hosts={hosts} onChange={setHosts} />
      </div>
      <details className="env-advanced-options">
        <summary>Advanced options</summary>
        <label className="env-field">
          <span className="env-field-label">Options (JSON)</span>
          <textarea className="field-input env-options-input" aria-label="Options JSON" rows={4} spellCheck={false} value={optionsText} onChange={(event) => setOptionsText(event.target.value)} />
          <span className="env-field-help">Connection-specific settings, as a JSON object. Keys starting with <code>_apex_</code> are reserved for global administrators.</span>
        </label>
      </details>
      {!optionsParse.ok && <p className="env-form-error" role="alert">{optionsParse.message}</p>}
      {!canApproveTarget && hasReservedOptions && <p className="env-form-error" role="alert">Options beginning with _apex_ require a global administrator.</p>}
      {create.isError && <div className="env-inline-error" role="alert"><span>Create failed: {create.error.message}</span></div>}
      <div className="env-panel-actions">
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClose} disabled={create.isPending}>{justCreated ? 'Skip for now' : 'Cancel'}</button>
        <button type="submit" className="btn btn-primary btn-sm" disabled={!canSubmit}>{create.isPending ? 'Creating…' : 'Create environment'}</button>
      </div>
    </form>
  )
}

function EnvironmentRow({ environment, applicationName, canMutate, onDelete }: {
  environment: Environment
  /** Disambiguates the row menu's name: environment names repeat across applications. */
  applicationName: string
  canMutate: boolean
  onDelete: (environment: Environment) => void
}) {
  const navigate = useNavigate()
  const writePending = usePendingMutationCount(environmentWriteMutationKey(environment.id)) > 0
  const path = `/environments/${environment.id}`
  const scannedAt = environment.last_snapshot?.scanned_at
  const hostNames = environment.hosts.map(host => host.hostname).join(', ')
  return (
    <tr className="env-row" data-testid={`env-row-${environment.id}`}>
      <td className="env-name-cell"><Link className="env-name-link" to={path}>{environment.name}</Link></td>
      <td><TargetBadge environment={environment} /></td>
      <td>
        {environment.base_url
          ? <span className="env-base-url" title={environment.base_url}>{targetHost(environment.base_url)}</span>
          : <span className="env-muted">{EM_DASH}</span>}
      </td>
      <td>{kindLabel(environment.kind) ?? <span className="env-muted">{EM_DASH}</span>}</td>
      <td className="num" title={hostNames || undefined}>{environment.hosts.length}</td>
      <td className="env-time" title={scannedAt ? formatTimestamp(scannedAt) : undefined}>{scannedAt ? formatRelative(scannedAt) : <span className="env-muted">Never</span>}</td>
      <td className="env-actions-cell">
        <OverflowMenu
          label={`Environment actions: ${environment.name} in ${applicationName}`}
          items={[
            { label: 'Open', onSelect: () => void navigate(path) },
            ...(canMutate
              ? [
                  { label: 'Edit', onSelect: () => void navigate(`${path}?edit=1`), disabled: writePending },
                  { label: 'Delete…', onSelect: () => onDelete(environment), disabled: writePending },
                ]
              : []),
          ]}
        />
      </td>
    </tr>
  )
}

type ApplicationDialog = { kind: 'edit' | 'archive' | 'delete'; application: Application; environmentCount: number }

function ApplicationBlock({
  group,
  accessibleName,
  totalEnvironments,
  canMutate,
  canDelete,
  creating,
  justCreated,
  canApproveTarget,
  onAddEnvironment,
  onCloseCreate,
  onDeleteEnvironment,
  onApplicationDialog,
}: {
  group: EnvironmentGroup
  /** The application's name, plus its project when another project has an application of the same name. */
  accessibleName: string
  totalEnvironments: number
  canMutate: boolean
  canDelete: boolean
  creating: boolean
  justCreated: boolean
  canApproveTarget: boolean
  onAddEnvironment: () => void
  onCloseCreate: () => void
  onDeleteEnvironment: (environment: Environment) => void
  onApplicationDialog: (dialog: ApplicationDialog) => void
}) {
  const application = group.application
  const archived = Boolean(application?.archived_at)
  const menu = application && canMutate ? [
    { label: 'Edit details…', onSelect: () => onApplicationDialog({ kind: 'edit', application, environmentCount: totalEnvironments }) },
    { label: archived ? 'Unarchive…' : 'Archive…', onSelect: () => onApplicationDialog({ kind: 'archive', application, environmentCount: totalEnvironments }) },
    ...(canDelete ? [{ label: 'Delete…', onSelect: () => onApplicationDialog({ kind: 'delete', application, environmentCount: totalEnvironments }) }] : []),
  ] : []
  const canAdd = Boolean(application) && canMutate && !archived && !creating
  // When the inline form closes and focus was lost with it (Cancel, Escape), put it back on this
  // application's Add button. Opening another application's form keeps focus where that form put it.
  const addButton = useRef<HTMLButtonElement>(null)
  const wasCreating = useRef(creating)
  useEffect(() => {
    const active = document.activeElement
    if (wasCreating.current && !creating && (!active || active === document.body)) addButton.current?.focus()
    wasCreating.current = creating
  }, [creating])
  return (
    <section className={`env-group${archived ? ' env-group--archived' : ''}`} aria-label={`Application ${accessibleName}`}>
      <header className="env-group-header">
        <div className="env-group-heading">
          <h3 className="env-group-title"><Link to={`/environments?application=${encodeURIComponent(group.key)}`}>{group.label}</Link></h3>
          {archived && <span className="topbar-meta-chip">Archived</span>}
          <span className="env-muted env-group-count">{totalEnvironments} {totalEnvironments === 1 ? 'environment' : 'environments'}</span>
        </div>
        <div className="env-group-actions">
          {canAdd && group.environments.length > 0 && <button ref={addButton} type="button" className="btn btn-ghost btn-sm" onClick={onAddEnvironment}>Add environment</button>}
          {menu.length > 0 && <OverflowMenu label={`Application actions: ${accessibleName}`} items={menu} />}
        </div>
      </header>
      {application?.description && <p className="env-group-description">{application.description}</p>}
      {creating && application && (
        <CreateEnvironmentPanel application={application} siblingNames={group.environments.map(environment => environment.name)}
          canApproveTarget={canApproveTarget} justCreated={justCreated} onClose={onCloseCreate} />
      )}
      {group.environments.length === 0 ? (
        !creating && <div className="glass-panel env-group-empty">
          <span>No environments yet.</span>
          {canAdd && <button ref={addButton} type="button" className="btn btn-secondary btn-sm" onClick={onAddEnvironment}>Add first environment</button>}
        </div>
      ) : (
        <div className="data-table-wrap">
          <table className="data-table striped env-table">
            <colgroup>
              <col className="env-col-name" /><col className="env-col-target" /><col className="env-col-host" />
              <col className="env-col-kind" /><col className="env-col-hosts" /><col className="env-col-scan" /><col className="env-col-actions" />
            </colgroup>
            <thead>
              <tr>
                <th>Name</th>
                <th>Target</th>
                <th>Target host</th>
                <th>Platform</th>
                <th className="num">Hosts</th>
                <th>Last scan</th>
                <th className="env-actions-cell"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {group.environments.map((environment) => (
                <EnvironmentRow key={environment.id} environment={environment} applicationName={accessibleName} canMutate={canMutate && !archived} onDelete={onDeleteEnvironment} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

export function EnvironmentsPage() {
  useTimestampPreference()
  const [searchParams, setSearchParams] = useSearchParams()
  const search = searchParams.get('q') ?? ''
  const projectFilter = searchParams.get('project') ?? ''
  const applicationFilter = searchParams.get('application')
  const readiness = (['approved', 'needs-approval', 'no-target'].includes(searchParams.get('target') ?? '') ? searchParams.get('target') : '') as ReadinessFilter
  const showArchived = searchParams.get('archived') === '1'

  // Always fetch archived applications: hiding them is a view choice, and their environments
  // (still returned by the environments list) must stay with them rather than look orphaned.
  const applications = useApplicationsIndex({ includeArchived: true })
  const labels = useProjectLabels()
  const projectCaption = (id: string) => `${(!labels.isError && labels.data?.find(row => row.project_id === id)?.name) || 'Project name unavailable'} (${id})`
  const environments = useEnvironmentsIndex()
  const consumer = useConsumer()
  const canMutate = canLaunchPipelines(consumer)
  const canApproveTarget = isGlobalAdmin(consumer)
  const projectWideScopes = Array.from(new Set((consumer?.scopes ?? []).filter((scope) => !scope.app_id).map((scope) => scope.project_id))).sort()
  const canCreateApplication = canMutate && (isGlobalAdmin(consumer) || projectWideScopes.length > 0)
  const applicationProjectOptions = (consumer?.scopes.length ?? 0) > 0 ? projectWideScopes : null

  const [creatingApplication, setCreatingApplication] = useState(false)
  const [creatingEnvironmentFor, setCreatingEnvironmentFor] = useState<{ applicationId: string; justCreated: boolean } | null>(null)
  const [deleting, setDeleting] = useState<Environment | null>(null)
  const [applicationDialog, setApplicationDialog] = useState<ApplicationDialog | null>(null)
  const [collapsed, setCollapsed] = useState<string[]>(readCollapsed)

  function setParam(key: string, value: string) {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (value) next.set(key, value)
      else next.delete(key)
      return next
    }, { replace: true })
  }
  function clearFilters() {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      for (const key of ['q', 'project', 'target', 'application']) next.delete(key)
      return next
    }, { replace: true })
  }
  function toggleProject(project: string) {
    setCollapsed(current => {
      const next = current.includes(project) ? current.filter(item => item !== project) : [...current, project]
      writeCollapsed(next)
      return next
    })
  }

  const shownApplications = useMemo(
    () => (applications.data ?? []).filter(application => showArchived || !application.archived_at),
    [applications.data, showArchived],
  )
  const allGroups = useMemo(() => {
    const hidden = new Set((applications.data ?? []).filter(application => !showArchived && application.archived_at).map(application => application.id))
    return groupEnvironments(shownApplications, (environments.data ?? []).filter(environment => !hidden.has(environment.application_id)))
  }, [applications.data, environments.data, shownApplications, showArchived])
  // Same-named applications in different projects (ACC in several) need the project in their accessible names.
  const ambiguousNames = useMemo(() => {
    const projectsByName = new Map<string, Set<string>>()
    for (const group of allGroups) projectsByName.set(group.label, (projectsByName.get(group.label) ?? new Set()).add(group.project))
    return new Set([...projectsByName].filter(([, projects]) => projects.size > 1).map(([name]) => name))
  }, [allGroups])
  const totals = useMemo(() => new Map(allGroups.map(group => [group.key, group.environments.length])), [allGroups])
  const projects = useMemo(
    () => projectGroups(
      allGroups.filter(group => (!applicationFilter || group.key === applicationFilter) && (!projectFilter || group.project === projectFilter)),
      { search, readiness, projectName: id => (!labels.isError && labels.data?.find(row => row.project_id === id)?.name) || '' },
    ),
    [allGroups, applicationFilter, projectFilter, search, readiness, labels.data, labels.isError],
  )
  const visibleEnvironments = (environments.data ?? []).filter(environment => allGroups.some(group => group.key === environment.application_id))
  const needsApproval = visibleEnvironments.filter(environment => targetStatus(environment) === 'needs-approval').length
  const filterCount = Number(Boolean(search.trim())) + Number(Boolean(projectFilter)) + Number(Boolean(readiness)) + Number(Boolean(applicationFilter))
  const filtered = filterCount > 0
  const matchedEnvironments = projects.reduce((sum, project) => sum + project.environmentCount, 0)

  const isPending = applications.isPending || environments.isPending
  const queryError = environments.error ?? applications.error
  const hasCachedData = Boolean(applications.data && environments.data)
  const filteredApplication = applicationFilter ? allGroups.find(group => group.key === applicationFilter) : undefined

  return (
    <section className="env-page animate-enter">
      <header className="env-toolbar glass-panel">
        <TableToolbar label="Application filters" filterCount={filterCount} onClear={clearFilters}>
          <label className="ux-field">Search applications and environments
            <input type="search" className="field-input" placeholder="Name or target host" value={search} onChange={(event) => setParam('q', event.target.value)} />
          </label>
          <label className="ux-field">Project
            <select className="field-select" value={projectFilter} onChange={(event) => setParam('project', event.target.value)}>
              <option value="">All projects</option>
              {[...new Set(shownApplications.map((app) => app.project_id))].sort().map((project) => <option key={project} value={project}>{projectCaption(project)}</option>)}
            </select>
          </label>
          <label className="ux-field">Target
            <select className="field-select" value={readiness} onChange={(event) => setParam('target', event.target.value)}>
              <option value="">Any status</option>
              <option value="approved">Approved (ready for external engines)</option>
              <option value="needs-approval">Needs approval</option>
              <option value="no-target">No target URL</option>
            </select>
          </label>
          <label className="env-archived-toggle">
            <input type="checkbox" checked={showArchived} onChange={(event) => setParam('archived', event.target.checked ? '1' : '')} />
            Show archived applications
          </label>
        </TableToolbar>
        {canCreateApplication && (
          <button type="button" className="btn btn-primary btn-sm env-new-application" onClick={() => setCreatingApplication(true)}>New application</button>
        )}
      </header>
      {hasCachedData && (
        <p className="env-summary">
          {allGroups.length} {allGroups.length === 1 ? 'application' : 'applications'} · {visibleEnvironments.length} {visibleEnvironments.length === 1 ? 'environment' : 'environments'}
          {filtered && !filteredApplication && <> · {matchedEnvironments} {matchedEnvironments === 1 ? 'environment matches' : 'environments match'}</>}
          {needsApproval > 0 && <> · <button type="button" className="env-summary-link" onClick={() => setParam('target', 'needs-approval')}>{needsApproval} need target approval</button></>}
          {filteredApplication && <> · Showing {filteredApplication.label} only. <Link to="/environments">All applications</Link></>}
        </p>
      )}

      {creatingApplication && (
        <CreateApplicationDialog
          projectOptions={applicationProjectOptions}
          existing={applications.data ?? []}
          onClose={() => setCreatingApplication(false)}
          onCreated={(application) => {
            setCreatingApplication(false)
            setSearchParams(prev => {
              const next = new URLSearchParams(prev)
              for (const key of ['q', 'target', 'application']) next.delete(key)
              if (next.get('project')) next.set('project', application.project_id)
              return next
            }, { replace: true })
            setCollapsed(current => current.filter(project => project !== application.project_id))
            setCreatingEnvironmentFor({ applicationId: application.id, justCreated: true })
          }}
        />
      )}

      {queryError && hasCachedData && (
        <CachedDataWarning error={queryError} onRetry={() => { void applications.refetch(); void environments.refetch() }} />
      )}

      {isPending ? (
        <Skeleton label="Loading environments" rows={SKELETON_ROWS} />
      ) : queryError && !hasCachedData ? (
        <ProblemCard title="Environments unavailable" message={queryError.message} onRetry={() => { void applications.refetch(); void environments.refetch() }} />
      ) : projects.length === 0 ? (
        <StateCard kind={filtered ? 'filtered' : 'empty'}
          title={filtered ? 'No matching applications' : 'No applications yet'}
          action={filtered
            ? <button type="button" className="btn btn-secondary" onClick={clearFilters}>Show all applications</button>
            : canCreateApplication ? <button type="button" className="btn btn-primary" onClick={() => setCreatingApplication(true)}>New application</button> : null}>
          <p>{filtered ? 'No applications or environments match these filters.' : canCreateApplication
            ? 'Create an application first, then add the environments used by tests.'
            : consumer?.role !== 'viewer' ? 'Application creation requires project-wide access.'
              : 'Your role has read-only access. Ask a project operator to create an application.'}</p>
        </StateCard>
      ) : (
        projects.map((project) => {
          const open = filtered || !collapsed.includes(project.project)
          const bodyId = `env-project-${project.project}`
          return (
            <section key={project.project} className="env-project" aria-label={`Project ${projectCaption(project.project)}`}>
              <header className="env-project-header">
                <button type="button" className="env-project-toggle" aria-expanded={open} aria-controls={open ? bodyId : undefined} onClick={() => toggleProject(project.project)} disabled={filtered}
                  aria-label={`${open ? 'Collapse' : 'Expand'} project ${projectCaption(project.project)}`}>
                  <span aria-hidden="true">{open ? '▾' : '▸'}</span>
                </button>
                <div className="env-project-title">{project.project ? <ProjectIdentity projectId={project.project} editable headingLevel="h2" /> : <h2 className="project-identity-heading">Unknown project</h2>}</div>
                <span className="env-muted env-project-counts">
                  {project.applications.length} {project.applications.length === 1 ? 'application' : 'applications'} · {project.environmentCount} {project.environmentCount === 1 ? 'environment' : 'environments'}
                  {project.needsApproval > 0 && <> · <span className="env-needs-approval">{project.needsApproval} need approval</span></>}
                </span>
              </header>
              {open && (
                <div id={bodyId} className="env-project-body">
                  {project.applications.map((group) => (
                    <ApplicationBlock
                      key={group.key}
                      group={group}
                      accessibleName={ambiguousNames.has(group.label) ? `${group.label} (${projectCaption(group.project)})` : group.label}
                      totalEnvironments={totals.get(group.key) ?? group.environments.length}
                      canMutate={canMutateAudience(consumer, group.project, group.key)}
                      canDelete={consumer?.role === 'admin' && canMutateAudience(consumer, group.project, group.key)}
                      creating={creatingEnvironmentFor?.applicationId === group.key}
                      justCreated={creatingEnvironmentFor?.applicationId === group.key && creatingEnvironmentFor.justCreated}
                      canApproveTarget={canApproveTarget}
                      onAddEnvironment={() => setCreatingEnvironmentFor({ applicationId: group.key, justCreated: false })}
                      onCloseCreate={() => setCreatingEnvironmentFor(null)}
                      onDeleteEnvironment={setDeleting}
                      onApplicationDialog={setApplicationDialog}
                    />
                  ))}
                </div>
              )}
            </section>
          )
        })
      )}

      {!isPending && !queryError && !canMutate && projects.length > 0 && (
        <p className="env-permission-note">Your role has read-only access to applications and environments.</p>
      )}

      {deleting && <DeleteEnvironmentDialog environment={deleting} onClose={() => setDeleting(null)} />}
      {applicationDialog?.kind === 'edit' && (
        <EditApplicationDialog application={applicationDialog.application}
          siblingNames={(applications.data ?? []).filter(app => app.project_id === applicationDialog.application.project_id && app.id !== applicationDialog.application.id).map(app => app.name)}
          onClose={() => setApplicationDialog(null)} />
      )}
      {applicationDialog?.kind === 'archive' && (
        <ArchiveApplicationDialog application={applicationDialog.application} environmentCount={applicationDialog.environmentCount} onClose={() => setApplicationDialog(null)} />
      )}
      {applicationDialog?.kind === 'delete' && (
        <DeleteApplicationDialog application={applicationDialog.application} environmentCount={applicationDialog.environmentCount} onClose={() => setApplicationDialog(null)} />
      )}
    </section>
  )
}
