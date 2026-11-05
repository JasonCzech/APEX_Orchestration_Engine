/**
 * /admin/aifx — AIFx Context Services onboarding. Admin-gated; the route only
 * exists when the server reports features.aifx.
 *
 * Organizations name the GitHub Enterprise connection, owner and global-rules
 * repository (platform admins only). Applications add an APM ID plus their
 * team-documents and ACB repositories, pre-filled from the organization's
 * naming conventions and confirmed by the admin. Validate is a dry run of
 * exactly what a launch would pin: commit SHAs, declared entries, and every
 * problem by repository, path and line — never file content.
 */
import { useMemo, useState, type FormEvent } from 'react'
import { Link } from 'react-router'

import {
  fetchAifxSuggestion,
  useAifxOrganizations,
  useAifxSources,
  useCreateAifxOrganization,
  useDeleteAifxOrganization,
  useDeleteAifxSource,
  usePreviewAifxSource,
  usePutAifxSource,
  useUpdateAifxOrganization,
  type AifxApplicationSource,
  type AifxApplicationSourceWrite,
  type AifxOrganization,
  type AifxPreview,
} from '@/api/hooks/useAifx'
import { useConnectionsIndex } from '@/api/hooks/useConnections'
import { useApplicationsIndex } from '@/api/hooks/useEnvironments'
import { useAuth, useConsumer } from '@/auth/AuthProvider'
import { isGlobalAdmin } from '@/auth/RequireRole'
import { ProblemCard } from '@/components/ProblemCard'
import { Skeleton } from '@/components/ux/Skeleton'
import { StateCard } from '@/components/ux/StateCard'

import { AdminGate, TogglePill } from './adminShared'
import './admin.css'

const ROLE_LABEL: Record<string, string> = {
  'global-rules': 'Global rules',
  'team-documents': 'Team documents',
  'application-context': 'Application context (ACB)',
}

export function AifxPreviewPanel({ preview }: { preview: AifxPreview }) {
  return (
    <section className="adm-aifx-preview" aria-label="AIFx preview" data-testid="aifx-preview">
      <p className={preview.ok ? 'adm-inline-ok' : 'adm-inline-error'} role="status">
        {preview.ok
          ? `Ready: ${preview.sources.length} source${preview.sources.length === 1 ? '' : 's'} pinned, ${preview.total_chars.toLocaleString()} characters.`
          : preview.sources.length === 0
            ? 'Nothing to pin: map a team-documents or ACB repository, or set the organization’s global rules.'
            : `${preview.issues.length} problem${preview.issues.length === 1 ? '' : 's'} to fix before launches can pin this application.`}
      </p>
      {preview.sources.map((source) => (
        <div key={`${source.role}:${source.repo}`} className="adm-aifx-source">
          <h3 className="adm-aifx-source-title">
            <span className={source.ok ? 'status-badge success' : 'status-badge danger'}>{ROLE_LABEL[source.role] ?? source.role}</span>{' '}
            <code>{source.repo}</code> @ <code>{source.ref}</code>
            {source.commit_sha && (
              <>
                {' → '}
                {source.web_url ? (
                  <a href={source.web_url} target="_blank" rel="noreferrer"><code>{source.commit_sha.slice(0, 7)}</code></a>
                ) : (
                  <code>{source.commit_sha.slice(0, 7)}</code>
                )}
              </>
            )}
          </h3>
          {(source.entries ?? []).length > 0 && (
            <ul className="adm-aifx-entries">
              {(source.entries ?? []).map((entry) => (
                <li key={entry.id}>
                  <span className="dash-context-chip adm-chip-muted">{entry.type}</span> {entry.title}{' '}
                  <span className="adm-muted">
                    {entry.path} · {Array.isArray(entry.phases) ? entry.phases.join(', ') : 'all phases'} · {entry.chars.toLocaleString()} chars
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}
      {preview.issues.length > 0 && (
        <ul className="adm-aifx-issues" aria-label="Problems">
          {preview.issues.map((issue, index) => (
            <li key={index} className="adm-inline-error">
              <code>
                {issue.repo}
                {issue.path ? `:${issue.path}` : ''}
                {issue.lines?.length ? `:${issue.lines.join(',')}` : ''}
              </code>{' '}
              — {issue.message}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function OrganizationForm({ onClose }: { onClose: () => void }) {
  const connections = useConnectionsIndex()
  const create = useCreateAifxOrganization()
  const usable = useMemo(
    () => (connections.data ?? []).filter((connection) => connection.kind === 'source_control' && !connection.project_id),
    [connections.data],
  )
  const [name, setName] = useState('')
  const [connectionId, setConnectionId] = useState('')
  const [owner, setOwner] = useState('')
  const [globalRules, setGlobalRules] = useState('global-rules')
  const [teamPattern, setTeamPattern] = useState('{apm}-team-documents')
  const [acbPattern, setAcbPattern] = useState('{apm}-acb')
  const [ref, setRef] = useState('main')
  const missing = !name.trim() ? 'Name the organization.' : !connectionId ? 'Choose a GitHub Enterprise connection.' : !owner.trim() ? 'Enter the GitHub owner.' : null

  function submit(event: FormEvent) {
    event.preventDefault()
    if (missing) return
    create.mutate(
      {
        name: name.trim(),
        connection_id: connectionId,
        ghe_owner: owner.trim(),
        global_rules_repo: globalRules.trim() || null,
        team_documents_repo_pattern: teamPattern.trim() || null,
        acb_repo_pattern: acbPattern.trim() || null,
        default_ref: ref.trim() || 'main',
        enabled: true,
      },
      { onSuccess: onClose },
    )
  }

  return (
    <form className="glass-panel adm-panel" onSubmit={submit} aria-label="Onboard organization">
      <h2 className="adm-panel-title">Onboard organization</h2>
      <div className="adm-form-grid">
        <label className="adm-field">
          <span className="adm-field-label">Name</span>
          <input className="field-input" aria-label="Name" value={name} onChange={(event) => setName(event.target.value)} placeholder="Consumer Mobility" />
        </label>
        <label className="adm-field">
          <span className="adm-field-label">Connection</span>
          <select className="field-select" aria-label="Connection" value={connectionId} onChange={(event) => setConnectionId(event.target.value)}>
            <option value="">Select a global source-control connection…</option>
            {usable.map((connection) => (
              <option key={connection.id} value={connection.id}>
                {connection.name} ({connection.provider})
              </option>
            ))}
          </select>
          <span className="adm-field-help">
            {usable.length === 0 && !connections.isPending ? (
              <>No global source-control connection yet — <Link to="/admin/connections">create a GitHub Enterprise connection</Link>.</>
            ) : (
              'Global (not project-scoped): every project’s tests read these repositories.'
            )}
          </span>
        </label>
        <label className="adm-field">
          <span className="adm-field-label">GitHub owner</span>
          <input className="field-input" aria-label="GitHub owner" value={owner} onChange={(event) => setOwner(event.target.value)} placeholder="aifx" />
          <span className="adm-field-help">The GHE organization that holds the AIFx repositories.</span>
        </label>
        <label className="adm-field">
          <span className="adm-field-label">Global rules repository</span>
          <input className="field-input" aria-label="Global rules repository" value={globalRules} onChange={(event) => setGlobalRules(event.target.value)} />
        </label>
        <label className="adm-field">
          <span className="adm-field-label">Team documents convention</span>
          <input className="field-input" aria-label="Team documents convention" value={teamPattern} onChange={(event) => setTeamPattern(event.target.value)} />
          <span className="adm-field-help">{'{apm}'} is replaced by each application’s APM ID.</span>
        </label>
        <label className="adm-field">
          <span className="adm-field-label">ACB convention</span>
          <input className="field-input" aria-label="ACB convention" value={acbPattern} onChange={(event) => setAcbPattern(event.target.value)} />
        </label>
        <label className="adm-field">
          <span className="adm-field-label">Default ref</span>
          <input className="field-input" aria-label="Default ref" value={ref} onChange={(event) => setRef(event.target.value)} />
          <span className="adm-field-help">Branch or tag launches pin; a protected or release tag is safest.</span>
        </label>
      </div>
      {create.isError && <p className="adm-form-error" role="alert">{create.error.message}</p>}
      <div className="adm-panel-actions">
        <button type="submit" className="btn btn-primary btn-sm" disabled={Boolean(missing) || create.isPending}>
          {create.isPending ? 'Onboarding…' : 'Onboard organization'}
        </button>
        <button type="button" className="btn btn-secondary btn-sm" onClick={onClose}>Cancel</button>
        {missing && <span className="adm-muted">{missing}</span>}
      </div>
    </form>
  )
}

function OrganizationsPanel({ organizations, platformAdmin }: { organizations: AifxOrganization[]; platformAdmin: boolean }) {
  const connections = useConnectionsIndex()
  const update = useUpdateAifxOrganization()
  const remove = useDeleteAifxOrganization()
  const [creating, setCreating] = useState(false)
  const names = new Map((connections.data ?? []).map((connection) => [connection.id, connection.name]))
  return (
    <section className="glass-panel adm-card-panel" aria-label="AIFx organizations">
      <div className="adm-aifx-panel-head">
        <h2 className="adm-panel-title">Organizations</h2>
        {platformAdmin && !creating && (
          <button type="button" className="btn btn-primary btn-sm" onClick={() => setCreating(true)}>Onboard organization</button>
        )}
      </div>
      {creating && <OrganizationForm onClose={() => setCreating(false)} />}
      {organizations.length === 0 ? (
        <StateCard kind="unconfigured" title="No organizations onboarded" action={null}>
          {platformAdmin
            ? 'Onboard the GitHub Enterprise organization that holds your global rules, team documents and ACB repositories.'
            : 'A platform admin onboards organizations; you can then map applications to them.'}
        </StateCard>
      ) : (
        <div className="data-table-wrap">
          <table className="data-table striped" aria-label="Organizations">
            <thead>
              <tr>
                <th>Name</th>
                <th>GitHub owner</th>
                <th>Connection</th>
                <th>Global rules</th>
                <th>Conventions</th>
                <th>Default ref</th>
                <th>Enabled</th>
                {platformAdmin && <th><span className="sr-only">Actions</span></th>}
              </tr>
            </thead>
            <tbody>
              {organizations.map((org) => (
                <tr key={org.id}>
                  <td className="strong">{org.name}</td>
                  <td><code>{org.ghe_owner}</code></td>
                  <td>
                    {names.has(org.connection_id) ? (
                      <Link to={`/admin/connections/${org.connection_id}`}>{names.get(org.connection_id)}</Link>
                    ) : (
                      <span className="adm-muted">{org.connection_id}</span>
                    )}
                  </td>
                  <td>{org.global_rules_repo ? <code>{org.global_rules_repo}</code> : <span className="adm-muted">none</span>}</td>
                  <td className="adm-muted">
                    {[org.team_documents_repo_pattern, org.acb_repo_pattern].filter(Boolean).join(' · ') || 'none'}
                  </td>
                  <td><code>{org.default_ref}</code></td>
                  <td>
                    {platformAdmin ? (
                      <TogglePill
                        label={`${org.name} enabled`}
                        enabled={org.enabled}
                        pending={update.isPending}
                        onToggle={() => update.mutate({ organizationId: org.id, body: { enabled: !org.enabled } })}
                      />
                    ) : (
                      <span className={org.enabled ? 'status-badge success' : 'status-badge danger'}>{org.enabled ? 'Enabled' : 'Disabled'}</span>
                    )}
                  </td>
                  {platformAdmin && (
                    <td>
                      <button type="button" className="btn btn-secondary btn-sm" disabled={remove.isPending} onClick={() => remove.mutate(org.id)}>
                        Remove
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {remove.isError && <p className="adm-form-error" role="alert">{remove.error.message}</p>}
      {update.isError && <p className="adm-form-error" role="alert">{update.error.message}</p>}
    </section>
  )
}

interface MappingEditorProps {
  organizations: AifxOrganization[]
  applications: { id: string; name: string }[]
  existing: AifxApplicationSource | null
  onClose: () => void
}

function MappingEditor({ organizations, applications, existing, onClose }: MappingEditorProps) {
  const put = usePutAifxSource()
  const preview = usePreviewAifxSource()
  const [applicationId, setApplicationId] = useState(existing?.application_id ?? '')
  const [organizationId, setOrganizationId] = useState(existing?.organization_id ?? organizations[0]?.id ?? '')
  const [apmId, setApmId] = useState(existing?.apm_id ?? '')
  const [teamRepo, setTeamRepo] = useState(existing?.team_documents_repo ?? '')
  const [acbRepo, setAcbRepo] = useState(existing?.acb_repo ?? '')
  const [ref, setRef] = useState(existing?.ref ?? '')
  const [suggestError, setSuggestError] = useState<string | null>(null)
  const organization = organizations.find((org) => org.id === organizationId)
  const missing = !applicationId ? 'Choose an application.' : !organizationId ? 'Choose an organization.' : !apmId.trim() ? 'Enter the APM ID.' : null

  const draft = (): AifxApplicationSourceWrite => ({
    organization_id: organizationId,
    apm_id: apmId.trim(),
    team_documents_repo: teamRepo.trim() || null,
    acb_repo: acbRepo.trim() || null,
    ref: ref.trim() || null,
    enabled: existing?.enabled ?? true,
  })
  // A result describes the draft it validated; once the form changes it no longer applies.
  const draftKey = JSON.stringify([applicationId, draft()])
  const validatedKey = preview.variables ? JSON.stringify([preview.variables.applicationId, preview.variables.draft]) : null
  const previewStale = preview.data !== undefined && validatedKey !== draftKey

  async function suggest() {
    setSuggestError(null)
    try {
      const suggestion = await fetchAifxSuggestion(applicationId, organizationId, apmId.trim())
      setTeamRepo(suggestion.team_documents_repo ?? '')
      setAcbRepo(suggestion.acb_repo ?? '')
    } catch (error) {
      setSuggestError(error instanceof Error ? error.message : 'Suggestion failed')
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (missing) return
    put.mutate({ applicationId, body: draft() }, { onSuccess: onClose })
  }

  return (
    <form className="glass-panel adm-panel" onSubmit={submit} aria-label={existing ? 'Edit application mapping' : 'Onboard application'}>
      <h2 className="adm-panel-title">{existing ? `Edit ${applications.find((app) => app.id === existing.application_id)?.name ?? 'application'}` : 'Onboard application'}</h2>
      <div className="adm-form-grid">
        <label className="adm-field">
          <span className="adm-field-label">Application</span>
          <select className="field-select" aria-label="Application" value={applicationId} disabled={Boolean(existing)} onChange={(event) => setApplicationId(event.target.value)}>
            <option value="">Select an application…</option>
            {applications.map((app) => (
              <option key={app.id} value={app.id}>{app.name}</option>
            ))}
          </select>
        </label>
        <label className="adm-field">
          <span className="adm-field-label">Organization</span>
          <select className="field-select" aria-label="Organization" value={organizationId} onChange={(event) => setOrganizationId(event.target.value)}>
            {organizations.map((org) => (
              <option key={org.id} value={org.id}>{org.name}</option>
            ))}
          </select>
        </label>
        <label className="adm-field">
          <span className="adm-field-label">APM ID</span>
          <input className="field-input" aria-label="APM ID" value={apmId} onChange={(event) => setApmId(event.target.value)} placeholder="APM0012345" />
        </label>
        <div className="adm-field">
          <span className="adm-field-label">From conventions</span>
          <button type="button" className="btn btn-secondary btn-sm" disabled={Boolean(missing)} onClick={() => void suggest()}>
            Suggest repositories
          </button>
          <span className="adm-field-help">Fills the names below from {organization?.name ?? 'the organization'}’s conventions; confirm before saving.</span>
        </div>
        <label className="adm-field">
          <span className="adm-field-label">Team documents repository</span>
          <input className="field-input" aria-label="Team documents repository" value={teamRepo} onChange={(event) => setTeamRepo(event.target.value)} />
          <span className="adm-field-help">With global rules, the source of this team’s platform policy.</span>
        </label>
        <label className="adm-field">
          <span className="adm-field-label">ACB repository</span>
          <input className="field-input" aria-label="ACB repository" value={acbRepo} onChange={(event) => setAcbRepo(event.target.value)} />
          <span className="adm-field-help">Application Context Builder: the source of truth for the application’s architecture.</span>
        </label>
        <label className="adm-field">
          <span className="adm-field-label">Ref (optional)</span>
          <input className="field-input" aria-label="Ref" value={ref} onChange={(event) => setRef(event.target.value)} placeholder={organization?.default_ref ?? 'main'} />
        </label>
      </div>
      {suggestError && <p className="adm-form-error" role="alert">{suggestError}</p>}
      {put.isError && <p className="adm-form-error" role="alert">{put.error.message}</p>}
      {preview.isError && !previewStale && <p className="adm-form-error" role="alert">{preview.error.message}</p>}
      <div className="adm-panel-actions">
        <button type="submit" className="btn btn-primary btn-sm" disabled={Boolean(missing) || put.isPending}>
          {put.isPending ? 'Saving…' : 'Save mapping'}
        </button>
        <button type="button" className="btn btn-secondary btn-sm" disabled={Boolean(missing) || preview.isPending} onClick={() => preview.mutate({ applicationId, draft: draft() })}>
          {preview.isPending ? 'Validating…' : 'Validate'}
        </button>
        <button type="button" className="btn btn-secondary btn-sm" onClick={onClose}>Cancel</button>
        {missing && <span className="adm-muted">{missing}</span>}
      </div>
      {preview.data &&
        (previewStale ? (
          <p className="adm-muted" role="status" data-testid="aifx-preview-stale">
            The mapping changed since it was validated. Validate again to check this draft.
          </p>
        ) : (
          <AifxPreviewPanel preview={preview.data} />
        ))}
    </form>
  )
}

function ApplicationsPanel({ organizations, sources }: { organizations: AifxOrganization[]; sources: AifxApplicationSource[] }) {
  const applications = useApplicationsIndex()
  const preview = usePreviewAifxSource()
  const remove = useDeleteAifxSource()
  const [editing, setEditing] = useState<AifxApplicationSource | 'new' | null>(null)
  const [previewing, setPreviewing] = useState<string | null>(null)
  const orgNames = new Map(organizations.map((org) => [org.id, org.name]))
  const mapped = new Set(sources.map((source) => source.application_id))
  const allApps = (applications.data ?? []).map((app) => ({ id: app.id, name: app.name }))
  const unmapped = allApps.filter((app) => !mapped.has(app.id))
  const previewed = previewing ? sources.find((source) => source.application_id === previewing) : undefined
  const previewCurrent = previewed !== undefined && preview.variables?.applicationId === previewing

  return (
    <section className="glass-panel adm-card-panel" aria-label="AIFx applications">
      <div className="adm-aifx-panel-head">
        <h2 className="adm-panel-title">Applications</h2>
        {organizations.length > 0 && editing === null && (
          <button type="button" className="btn btn-primary btn-sm" onClick={() => setEditing('new')}>Onboard application</button>
        )}
      </div>
      {editing !== null && (
        <MappingEditor
          key={editing === 'new' ? 'new' : editing.application_id}
          organizations={organizations}
          applications={editing === 'new' ? unmapped : allApps}
          existing={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
        />
      )}
      {sources.length === 0 ? (
        <StateCard kind="empty" title="No applications onboarded" action={null}>
          {organizations.length === 0
            ? 'Onboard an organization first, then map applications to their APM ID and repositories.'
            : 'Map an application to its APM ID, team documents and ACB repository so its tests pick up AIFx context.'}
        </StateCard>
      ) : (
        <div className="data-table-wrap">
          <table className="data-table striped" aria-label="Onboarded applications">
            <thead>
              <tr>
                <th>Application</th>
                <th>APM ID</th>
                <th>Organization</th>
                <th>Team documents</th>
                <th>ACB</th>
                <th>Ref</th>
                <th><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {sources.map((source) => (
                <tr key={source.application_id} data-testid={`aifx-app-${source.application_id}`}>
                  <td className="strong">{source.application_name ?? source.application_id}</td>
                  <td><code>{source.apm_id}</code></td>
                  <td>{orgNames.get(source.organization_id) ?? source.organization_id}</td>
                  <td>{source.team_documents_repo ? <code>{source.team_documents_repo}</code> : <span className="adm-muted">none</span>}</td>
                  <td>{source.acb_repo ? <code>{source.acb_repo}</code> : <span className="adm-muted">none</span>}</td>
                  <td>{source.ref ? <code>{source.ref}</code> : <span className="adm-muted">default</span>}</td>
                  <td className="adm-aifx-actions">
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      disabled={preview.isPending}
                      onClick={() => {
                        setPreviewing(source.application_id)
                        preview.mutate({ applicationId: source.application_id })
                      }}
                    >
                      Validate
                    </button>
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      onClick={() => {
                        setPreviewing(null)
                        setEditing(source)
                      }}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      disabled={remove.isPending}
                      onClick={() => {
                        if (previewing === source.application_id) setPreviewing(null)
                        remove.mutate(source.application_id)
                      }}
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
      {remove.isError && <p className="adm-form-error" role="alert">{remove.error.message}</p>}
      {previewCurrent && (preview.isError || preview.data) && (
        <div className="adm-aifx-saved-preview">
          <h3 className="adm-aifx-source-title">Validation: {previewed.application_name ?? previewed.application_id}</h3>
          {preview.isError && <p className="adm-form-error" role="alert">{preview.error.message}</p>}
          {preview.data && <AifxPreviewPanel preview={preview.data} />}
        </div>
      )}
    </section>
  )
}

function AifxContent() {
  const consumer = useConsumer()
  const platformAdmin = isGlobalAdmin(consumer)
  const organizations = useAifxOrganizations()
  const sources = useAifxSources()

  return (
    <section className="adm-page animate-enter">
      <header className="adm-toolbar glass-panel">
        <div>
          <p className="adm-field-help">
            AIFx Context Services read agent rules, memories, skills and application context from GitHub Enterprise.
            Each test pins them at a commit when it launches; operators see the sources at every prompt review.
          </p>
        </div>
      </header>
      {organizations.isPending || sources.isPending ? (
        <Skeleton label="Loading AIFx onboarding" rows={4} />
      ) : organizations.isError ? (
        <ProblemCard title="AIFx organizations unavailable" message={organizations.error.message} onRetry={() => void organizations.refetch()} />
      ) : sources.isError ? (
        <ProblemCard title="AIFx applications unavailable" message={sources.error.message} onRetry={() => void sources.refetch()} />
      ) : (
        <>
          <OrganizationsPanel organizations={organizations.data} platformAdmin={platformAdmin} />
          <ApplicationsPanel organizations={organizations.data} sources={sources.data} />
        </>
      )}
    </section>
  )
}

function AifxDisabled() {
  return (
    <StateCard kind="unconfigured" title="AIFx context" action={<Link className="btn btn-secondary" to="/admin/connections">Open connections</Link>}>
      AIFx Context Services are not enabled on this server (APEX_AIFX_ENABLED).
    </StateCard>
  )
}

export function AifxPage() {
  const { state } = useAuth()
  const enabled = state.status === 'authenticated' && state.systemInfo.features.aifx === true
  return <AdminGate>{enabled ? <AifxContent /> : <AifxDisabled />}</AdminGate>
}
