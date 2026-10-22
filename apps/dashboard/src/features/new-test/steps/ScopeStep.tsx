import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { Dialog } from '@/components/Dialog'
import { useApplications, useEnvironments } from '@/api/hooks/useCatalog'
import { useApplicationsIndex } from '@/api/hooks/useEnvironments'
import { useOptionalConsumer, useOptionalAuth } from '@/auth/AuthProvider'
import { emptyTestScope, testScopeIssues, type TestParticipant, type TestScopeV1 } from '../testScope'
import type { StepProps } from '../NewRunWizard'

function ParticipantRow({ participant: p, primary, traffic, update, remove, choosePrimary, chooseTraffic }: {
  participant: TestParticipant; primary: boolean; traffic: boolean
  update: (p: TestParticipant) => void; remove: () => void
  choosePrimary: () => void; chooseTraffic: () => void
}) {
  const consumer = useOptionalConsumer()
  const index = useApplicationsIndex()
  const applications = useApplications(p.project_id || undefined)
  const environments = useEnvironments(p.application_id)
  const projects = [...new Set([...(index.data ?? []).map(a => a.project_id), ...(consumer?.scopes ?? []).map(s => s.project_id)])].sort()
  return <fieldset className="wizard-field">
    <legend>{p.layer === 'middle' ? 'Middle tier' : p.layer === 'frontend' ? 'Frontend' : 'Backend'} {primary && ' · Primary system'}{traffic && ' · Traffic entry'}</legend>
    <label>Project<select className="field-select" aria-label="Project" value={p.project_id} onChange={e => update({ ...p, project_id: e.target.value, application_id: '', environment_id: '' })}>
      <option value="">Select project…</option>{projects.map(id => <option key={id} value={id}>{id}</option>)}
    </select></label>
    <label>Application<select className="field-select" aria-label="Application" value={p.application_id} disabled={!p.project_id} onChange={e => update({ ...p, application_id: e.target.value, environment_id: '' })}>
      <option value="">Select application…</option>{(applications.data ?? []).map(a => <option key={a.id} value={a.id}>{a.name} · {p.project_id}</option>)}
    </select></label>
    <label>Environment<select className="field-select" aria-label="Environment" value={p.environment_id} disabled={!p.application_id} onChange={e => update({ ...p, environment_id: e.target.value })}>
      <option value="">Select environment…</option>{(environments.data ?? []).map(env => <option key={env.id} value={env.id}>{env.name}{env.target_approved ? ' · target approved' : ''}</option>)}
    </select></label>
    <label><input type="radio" name="primary-system" checked={primary} onChange={choosePrimary} />Primary system under test</label>
    <label><input type="radio" name="traffic-entry" checked={traffic} onChange={chooseTraffic} />Traffic entry</label>
    <button type="button" className="btn btn-secondary btn-sm" onClick={remove}>Remove participant</button>
    {(applications.isError || environments.isError) && <p role="alert">Catalog could not be loaded. <button type="button" onClick={() => { void applications.refetch(); void environments.refetch() }}>Retry</button></p>}
  </fieldset>
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
  function change(next: TestScopeV1) {
    const changedMembers = scope.participants.some(p => !next.participants.some(n => n.id === p.id && n.application_id === p.application_id && n.environment_id === p.environment_id))
    const ownerChanged = next.participants.find(p => p.id === next.primary_participant_id)?.project_id !== draft.scope.project_id
    if ((changedMembers && (draft.document_ids.length > 0 || Object.keys(draft.prompt_overrides).length > 0)) || (ownerChanged && (draft.work_items.length > 0 || draft.context_summary_ids.length > 0 || draft.config.golden_config_id))) setPending(next)
    else apply(next)
  }
  function add(layer: TestParticipant['layer']) {
    change({ ...scope, participants: [...scope.participants, { id: crypto.randomUUID(), layer, project_id: !draft.scope.test_scope ? draft.scope.project_id : '', application_id: !draft.scope.test_scope ? draft.scope.app_id ?? '' : '', environment_id: !draft.scope.test_scope ? draft.scope.environment_id ?? '' : '' }] })
  }
  const issues = testScopeIssues(scope, maxParticipants)
  return <section className="wizard-step wizard-scope" aria-label="Scope">
    {pending && <Dialog className="glass-panel wizard-confirm" overlayClassName="wizard-confirm-overlay" ariaLabel="Change test scope" onClose={() => setPending(null)}>
      <h2>Change test scope?</h2><p>Context tied to removed systems must be revalidated. Changing the owning project clears tickets, context summaries, and the selected preset.</p>
      <button type="button" onClick={() => setPending(null)}>Cancel</button><button type="button" onClick={() => apply(pending)}>Change scope</button>
    </Dialog>}
    <label className="wizard-field">Title<input id="wizard-title" className="field-input" required value={draft.title} onChange={e => onChange(p => ({ ...p, title: e.target.value }))} /></label>
    <label className="wizard-field">Request<textarea id="wizard-request" className="field-input" required rows={4} value={draft.request} onChange={e => onChange(p => ({ ...p, request: e.target.value }))} /></label>
    <p>Define one end-to-end system. Choose a primary system for objectives and reporting, and a separate traffic entry for live execution.</p>
    <p>Owning project: <strong>{draft.scope.project_id || 'Choose a primary system'}</strong></p>
    {!draft.scope.test_scope && draft.scope.app_id && <p role="status">Legacy selection: {draft.scope.project_id} / {draft.scope.app_id} / {draft.scope.environment_id ?? 'no environment'}. Add it below and explicitly assign its layer and primary role.</p>}
    {(['frontend', 'middle', 'backend'] as const).map(layer => <section key={layer} aria-label={`${layer} systems`}>
      <h2>{layer === 'middle' ? 'Middle tier · optional, multiple' : layer === 'frontend' ? 'Frontend · optional, one maximum' : 'Backend · exactly one'}</h2>
      {scope.participants.filter(p => p.layer === layer).map(p => <ParticipantRow key={p.id} participant={p} primary={scope.primary_participant_id === p.id} traffic={scope.traffic_entry_participant_id === p.id}
        update={participant => change({ ...scope, participants: scope.participants.map(row => row.id === p.id ? participant : row) })}
        remove={() => change({ ...scope, participants: scope.participants.filter(row => row.id !== p.id), primary_participant_id: scope.primary_participant_id === p.id ? '' : scope.primary_participant_id, traffic_entry_participant_id: scope.traffic_entry_participant_id === p.id ? null : scope.traffic_entry_participant_id })}
        choosePrimary={() => change({ ...scope, primary_participant_id: p.id })} chooseTraffic={() => change({ ...scope, traffic_entry_participant_id: p.id })} />)}
      <button type="button" className="btn btn-secondary" disabled={scope.participants.length >= maxParticipants || (layer !== 'middle' && scope.participants.some(p => p.layer === layer))} onClick={() => add(layer)}>Add {layer === 'middle' ? 'middle-tier system' : layer}</button>
    </section>)}
    {scope.traffic_entry_participant_id && <button type="button" onClick={() => change({ ...scope, traffic_entry_participant_id: null })}>Clear traffic entry</button>}
    <button type="button" className="btn btn-secondary" onClick={() => void queryClient.invalidateQueries({ queryKey: ['test-participant-catalog'] })}>Retry participant verification</button>
    {issues.length > 0 && <ul aria-label="Scope requirements">{issues.map(issue => <li key={issue}>{issue}</li>)}</ul>}
    <p><Link to="/environments">Manage applications and environments</Link></p>
  </section>
}
