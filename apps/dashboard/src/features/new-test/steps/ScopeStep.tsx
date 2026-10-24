import { useQueryClient } from '@tanstack/react-query'
import { useState, useSyncExternalStore } from 'react'
import { Link } from 'react-router'
import { Dialog } from '@/components/Dialog'
import { useApplications, useEnvironments } from '@/api/hooks/useCatalog'
import { useApplicationsIndex } from '@/api/hooks/useEnvironments'
import { useProjectLabels } from '@/api/hooks/useProjectLabels'
import { useOptionalConsumer, useOptionalAuth } from '@/auth/AuthProvider'
import { accessibleProjects, derivedProject, emptyTestScope, reachableProjects, testScopeIssues, type TestParticipant, type TestScopeV1 } from '../testScope'
import type { StepProps } from '../NewRunWizard'

function ParticipantRow({ participant: p, primary, traffic, owningProject, focusMissing, primaryId, update, remove, choosePrimary, chooseTraffic }: {
  participant: TestParticipant; primary: boolean; traffic: boolean
  /** The test's owning project: the primary system's. */
  owningProject: string
  /** The first incomplete row's empty field answers "complete every participant". */
  focusMissing: boolean; primaryId?: string
  update: (p: TestParticipant) => void; remove: () => void
  choosePrimary: () => void; chooseTraffic: () => void
}) {
  const consumer = useOptionalConsumer()
  const index = useApplicationsIndex()
  const labels = useProjectLabels()
  const projectName = (id: string) => (!labels.isError && labels.data?.find(row => row.project_id === id)?.name) || id
  const projectApplications = useApplications(primary ? p.project_id || undefined : undefined)
  const environments = useEnvironments(p.application_id)
  const visibleApps = (index.data ?? []).filter(app => !app.archived_at)
  const app = (index.data ?? []).find(item => item.id === p.application_id)
  const projects = reachableProjects(visibleApps, consumer).sort()
  // Only the primary system chooses a project; the others name an application and
  // reach it through the owning project when they can (F-17).
  const viaProjects = primary ? [] : accessibleProjects(app, consumer)
  const needsVia = !primary && Boolean(app) && !viaProjects.includes(owningProject) && viaProjects.length > 1
  const missing = primary && !p.project_id ? 'project' : !p.application_id ? 'application' : !p.environment_id ? 'environment' : needsVia && !p.project_id ? 'project' : null
  // Execution readiness links name the primary system's application and the
  // traffic entry's environment; those rows own the matching ids.
  const fieldId = (field: string) => (field === 'application' && primary) || (field === 'environment' && traffic) ? `wizard-${field}` : undefined
  const missingAlias = (field: string) => focusMissing && missing === field ? 'wizard-participant-missing' : undefined
  const layer = p.layer === 'middle' ? 'Middle tier' : p.layer === 'frontend' ? 'Frontend' : 'Backend'
  const applicationOptions = primary ? projectApplications.data ?? [] : visibleApps
  return <fieldset className="wizard-participant">
    <legend>{layer}{primary && ' · Primary system'}{traffic && ' · Traffic entry'}</legend>
    <div className="wizard-participant-fields">
      {primary && <label>Project<select id={fieldId('project')} data-focus-alias={missingAlias('project')} className="field-select" aria-label="Project" value={p.project_id} onChange={e => update({ ...p, project_id: e.target.value, application_id: '', environment_id: '' })}>
        <option value="">Select project…</option>{projects.map(id => <option key={id} value={id}>{projectName(id)}</option>)}
      </select></label>}
      <label>Application<select id={fieldId('application')} data-focus-alias={missingAlias('application')} className="field-select" aria-label="Application" value={p.application_id} disabled={primary && !p.project_id} onChange={e => {
        const chosen = (index.data ?? []).find(item => item.id === e.target.value)
        update({ ...p, application_id: e.target.value, environment_id: '', ...(primary ? {} : { project_id: derivedProject(chosen, owningProject, consumer) }) })
      }}>
        <option value="">Select application…</option>{applicationOptions.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
      </select></label>
      {needsVia && <label>Via project<select data-focus-alias={missingAlias('project')} className="field-select" aria-label="Via project" value={p.project_id} onChange={e => update({ ...p, project_id: e.target.value })}>
        <option value="">Select project…</option>{viaProjects.map(id => <option key={id} value={id}>{projectName(id)}</option>)}
      </select></label>}
      <label>Environment<select id={fieldId('environment')} data-focus-alias={missingAlias('environment')} className="field-select" aria-label="Environment" value={p.environment_id} disabled={!p.application_id} onChange={e => update({ ...p, environment_id: e.target.value })}>
        <option value="">Select environment…</option>{(environments.data ?? []).map(env => <option key={env.id} value={env.id}>{env.name} · {env.target_approved ? 'Target approved' : 'Target not approved'}</option>)}
      </select></label>
    </div>
    {!primary && p.project_id && p.project_id !== owningProject && !needsVia && <p className="wizard-caption">Reached through {projectName(p.project_id)}.</p>}
    <div className="wizard-participant-roles" role="group" aria-label={`${layer} role`}>
      <label className="wizard-role-chip"><input id={primaryId} type="radio" name="primary-system" checked={primary} onChange={choosePrimary} />Primary system under test</label>
      <label className="wizard-role-chip"><input type="radio" name="traffic-entry" checked={traffic} onChange={chooseTraffic} />Traffic entry</label>
    </div>
    <button type="button" className="btn btn-ghost btn-sm wizard-participant-remove" onClick={remove}>Remove {layer.toLowerCase()}</button>
    {(projectApplications.isError || environments.isError) && <p role="alert">Catalog could not be loaded. <button type="button" className="btn btn-secondary btn-sm" onClick={() => { void projectApplications.refetch(); void environments.refetch() }}>Retry</button></p>}
  </fieldset>
}

/** A copied pre-topology run names one system; offer it as the backend in one step. */
function LegacySelection({ draft, onUse }: { draft: StepProps['draft']; onUse: () => void }) {
  const applications = useApplications(draft.scope.project_id || undefined)
  const environments = useEnvironments(draft.scope.app_id ?? undefined)
  const application = applications.data?.find(app => app.id === draft.scope.app_id)?.name ?? draft.scope.app_id
  const environment = draft.scope.environment_id
    ? environments.data?.find(env => env.id === draft.scope.environment_id)?.name ?? draft.scope.environment_id
    : 'no environment'
  return <div className="tonal-card wizard-legacy-selection">
    <p>The copied run tested <strong>{application}</strong> ({draft.scope.project_id} · {environment}) before systems had layers and roles.</p>
    <button type="button" className="btn btn-secondary btn-sm" data-focus-alias="wizard-add-backend" onClick={onUse}>Use as backend (primary system)</button>
  </div>
}

export function ScopeStep({ draft, onChange }: StepProps) {
  const auth = useOptionalAuth()
  const maxParticipants = auth?.state.status === 'authenticated' ? auth.state.systemInfo.limits.max_test_participants : 32
  const queryClient = useQueryClient()
  const scope = draft.scope.test_scope ?? emptyTestScope()
  const [pending, setPending] = useState<TestScopeV1 | null>(null)
  function apply(next: TestScopeV1) {
    const primary = next.participants.find(p => p.id === next.primary_participant_id)
    const traffic = next.participants.find(p => p.id === next.traffic_entry_participant_id)
    onChange(prev => {
      const ownerChanged = primary?.project_id !== prev.scope.project_id
      return { ...prev,
        scope: { test_scope: next, project_id: primary?.project_id ?? '', app_id: primary?.application_id || null, environment_id: traffic?.environment_id || null },
        // Document ownership is verified again by the knowledge step and server.
        ...(ownerChanged ? { work_items: [], context_summary_ids: [], config: { ...prev.config, golden_config_id: null } } : {}),
      }
    })
    setPending(null)
  }
  const consumer = useOptionalConsumer()
  const index = useApplicationsIndex()
  /** Non-primary systems follow the owning project (F-17); explicit "via" choices stay. */
  function withDerivedProjects(next: TestScopeV1): TestScopeV1 {
    // Without the applications index there is nothing to derive from; keep what is there.
    if (!index.data) return next
    const owner = next.participants.find(p => p.id === next.primary_participant_id)?.project_id ?? ''
    return { ...next, participants: next.participants.map(p => {
      if (p.id === next.primary_participant_id || !p.application_id) return p
      const app = index.data.find(item => item.id === p.application_id)
      const reachable = accessibleProjects(app, consumer)
      if (owner && reachable.includes(owner)) return { ...p, project_id: owner }
      return reachable.includes(p.project_id) ? p : { ...p, project_id: derivedProject(app, owner, consumer) }
    }) }
  }
  function change(proposed: TestScopeV1) {
    const next = withDerivedProjects(proposed)
    const changedMembers = scope.participants.some(p => !next.participants.some(n => n.id === p.id && n.application_id === p.application_id && n.environment_id === p.environment_id))
    const ownerChanged = next.participants.find(p => p.id === next.primary_participant_id)?.project_id !== draft.scope.project_id
    if ((changedMembers && (draft.document_ids.length > 0 || Object.keys(draft.prompt_overrides).length > 0)) || (ownerChanged && (draft.work_items.length > 0 || draft.context_summary_ids.length > 0 || draft.config.golden_config_id))) setPending(next)
    else apply(next)
  }
  function add(layer: TestParticipant['layer']) {
    const id = crypto.randomUUID()
    change({ ...scope, participants: [...scope.participants, { id, layer, project_id: !draft.scope.test_scope ? draft.scope.project_id : '', application_id: !draft.scope.test_scope ? draft.scope.app_id ?? '' : '', environment_id: !draft.scope.test_scope ? draft.scope.environment_id ?? '' : '' }],
      // The first system is the primary one until the person picks another.
      primary_participant_id: scope.participants.some(p => p.id === scope.primary_participant_id) ? scope.primary_participant_id : id })
  }
  const issues = testScopeIssues(scope, maxParticipants)
  const labels = useProjectLabels()
  const ownerName = (!labels.isError && labels.data?.find(row => row.project_id === draft.scope.project_id)?.name) || draft.scope.project_id
  // Read (never observe or fetch) the readiness verification results so Retry
  // only appears when one of the current participants' checks failed. An
  // observer here would overwrite the queries' options and break the retry.
  const cache = queryClient.getQueryCache()
  const verificationKeys = JSON.stringify(scope.participants.map(p => [p.project_id, p.application_id, p.environment_id]))
  const verificationFailed = useSyncExternalStore(
    notify => cache.subscribe(notify),
    () => (JSON.parse(verificationKeys) as string[][]).some(key =>
      cache.find({ queryKey: ['test-participant-catalog', ...key], exact: true })?.state.status === 'error'),
  )
  const firstMissing = scope.participants.find(p => !p.project_id || !p.application_id || !p.environment_id)?.id
  const primaryTarget = scope.participants.find(p => p.layer === 'backend')?.id ?? scope.participants[0]?.id
  return <section className="wizard-step wizard-scope" aria-label="Scope">
    {pending && <Dialog className="glass-panel wizard-confirm" overlayClassName="wizard-confirm-overlay" ariaLabel="Change test scope" onClose={() => setPending(null)}>
      <h2>Change test scope?</h2><p>Context tied to removed systems must be revalidated. Changing the owning project clears tickets, context summaries, and the selected preset.</p>
      <button type="button" onClick={() => setPending(null)}>Cancel</button><button type="button" onClick={() => apply(pending)}>Change scope</button>
    </Dialog>}
    <label className="wizard-field">Title<input id="wizard-title" className="field-input" required value={draft.title} onChange={e => onChange(p => ({ ...p, title: e.target.value }))} /></label>
    <label className="wizard-field">Request<textarea id="wizard-request" className="field-input" required rows={4} value={draft.request} onChange={e => onChange(p => ({ ...p, request: e.target.value }))} /></label>
    <p>Define one end-to-end system. Choose a primary system for objectives and reporting, and a separate traffic entry for live execution.</p>
    <p>Owning project: <strong>{draft.scope.project_id ? ownerName : 'Choose the primary system and its project'}</strong></p>
    {!draft.scope.test_scope && draft.scope.app_id && <LegacySelection draft={draft} onUse={() => {
      const id = crypto.randomUUID()
      change({ ...scope, participants: [...scope.participants, { id, layer: 'backend', project_id: draft.scope.project_id, application_id: draft.scope.app_id ?? '', environment_id: draft.scope.environment_id ?? '' }],
        primary_participant_id: id, traffic_entry_participant_id: draft.scope.environment_id ? id : null })
    }} />}
    {(['frontend', 'middle', 'backend'] as const).map(layer => {
      const full = scope.participants.length >= maxParticipants || (layer !== 'middle' && scope.participants.some(p => p.layer === layer))
      return <section key={layer} className="wizard-layer" aria-label={`${layer} systems`}>
        <h3 className="wizard-layer-heading">{layer === 'middle' ? 'Middle tier' : layer === 'frontend' ? 'Frontend' : 'Backend'} <span className="wizard-caption">{layer === 'middle' ? 'optional · multiple' : layer === 'frontend' ? 'optional · one maximum' : 'required · exactly one'}</span></h3>
        {scope.participants.filter(p => p.layer === layer).map(p => <ParticipantRow key={p.id} participant={p} primary={scope.primary_participant_id === p.id} traffic={scope.traffic_entry_participant_id === p.id} owningProject={draft.scope.project_id}
          focusMissing={p.id === firstMissing} primaryId={p.id === primaryTarget ? 'wizard-primary' : undefined}
          update={participant => change({ ...scope, participants: scope.participants.map(row => row.id === p.id ? participant : row) })}
          remove={() => change({ ...scope, participants: scope.participants.filter(row => row.id !== p.id), primary_participant_id: scope.primary_participant_id === p.id ? '' : scope.primary_participant_id, traffic_entry_participant_id: scope.traffic_entry_participant_id === p.id ? null : scope.traffic_entry_participant_id })}
          choosePrimary={() => change({ ...scope, primary_participant_id: p.id })} chooseTraffic={() => change({ ...scope, traffic_entry_participant_id: p.id })} />)}
        {!full && <button type="button" id={`wizard-add-${layer}`} className="btn btn-secondary btn-sm" onClick={() => add(layer)}>Add {layer === 'middle' ? 'middle-tier system' : layer}</button>}
      </section>
    })}
    {scope.traffic_entry_participant_id && <button type="button" className="btn btn-ghost btn-sm" onClick={() => change({ ...scope, traffic_entry_participant_id: null })}>Clear traffic entry</button>}
    {verificationFailed && <button type="button" className="btn btn-secondary btn-sm" onClick={() => void queryClient.invalidateQueries({ queryKey: ['test-participant-catalog'] })}>Retry participant verification</button>}
    {issues.length > 0 && <ul aria-label="Scope requirements">{issues.map(issue => <li key={issue}>{issue}</li>)}</ul>}
    <p><Link to="/environments">Manage applications and environments</Link></p>
  </section>
}
