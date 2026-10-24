/**
 * Save as preset (F-02): store the configured test's settings for future runs.
 * - Visibility: Just me / <project> team (APEX test presets), or Organization
 *   (a LangGraph assistant; platform administrators only).
 * - Include: which wizard sections to keep. Title, request, tickets and
 *   knowledge are opt-in; they rarely carry over to another test.
 * - When the test started from a preset you may edit, Update it instead.
 */
import { useState, type FormEvent } from 'react'

import { useOptionalConsumer } from '@/auth/AuthProvider'
import { hasFullProjectScope, isGlobalAdmin } from '@/auth/RequireRole'
import { Dialog } from '@/components/Dialog'
import {
  useCreateTeamPreset,
  useSaveOrganizationPreset,
  useUpdateTeamPreset,
  type TeamPresetWrite,
} from '@/api/hooks/useTestPresets'
import { buildConfigurable, parseDraftPayload, type DraftPresetLink, type WizardDraft } from '@/features/new-test/wizardState'

import {
  DEFAULT_SAVE_SECTIONS,
  SECTION_IDS,
  SECTION_LABELS,
  applySections,
  fieldsFor,
  primaryApplication,
  sectionsFromDraft,
  type Preset,
  type SectionId,
} from './presetModel'
import { useFieldNames } from './useFieldNames'
import './presets.css'

type Visibility = 'personal' | 'project' | 'organization'

export function SavePresetDialog({ draft, existing, initialName, initialDescription, initialSections, onClose, onSaved }: {
  draft: WizardDraft
  /** The preset this test started from, when known (offers Update). */
  existing?: Preset | null
  initialName?: string
  initialDescription?: string | null
  initialSections?: SectionId[]
  onClose: () => void
  /** The saved preset, as a link the draft can now track. */
  onSaved: (link: DraftPresetLink) => void
}) {
  const consumer = useOptionalConsumer()
  const names = useFieldNames()
  const admin = isGlobalAdmin(consumer)
  const projectId = draft.scope.project_id.trim()
  const appId = primaryApplication(draft)
  const canShareWithTeam = Boolean(projectId) && (admin || hasFullProjectScope(consumer, projectId))
  const canUpdate = Boolean(existing && (existing.kind === 'team' ? existing.editable : admin))
  const [mode, setMode] = useState<'update' | 'new'>(canUpdate ? 'update' : 'new')
  const updating = mode === 'update' && canUpdate && existing
  const [name, setName] = useState(updating ? existing.name : initialName ?? draft.title.trim())
  const [description, setDescription] = useState(updating ? existing.description ?? '' : initialDescription ?? '')
  const [visibility, setVisibility] = useState<Visibility>(
    updating ? existing.visibility : initialName && admin ? 'organization' : canShareWithTeam ? 'project' : 'personal')
  const [forApplication, setForApplication] = useState(updating ? existing.applicationId !== null : Boolean(appId))
  const [include, setInclude] = useState<SectionId[]>(
    updating ? (Object.keys(existing.sections) as SectionId[]) : initialSections ?? DEFAULT_SAVE_SECTIONS)
  const [note, setNote] = useState('')
  const createTeam = useCreateTeamPreset()
  const updateTeam = useUpdateTeamPreset()
  const saveOrganization = useSaveOrganizationPreset()
  const pending = createTeam.isPending || updateTeam.isPending || saveOrganization.isPending
  const failure = createTeam.error ?? updateTeam.error ?? saveOrganization.error
  const projectName = projectId ? names.project(projectId) : ''
  // Updating keeps the preset on its own application unless the person turns it into a General one.
  const offeredApp = updating && existing.applicationId ? existing.applicationId : appId
  const movedProject = updating && existing.kind === 'team' && existing.projectId && projectId && existing.projectId !== projectId
    ? existing.projectId : null

  function chooseMode(next: 'update' | 'new') {
    setMode(next)
    if (next === 'update' && existing) {
      setName(existing.name)
      setDescription(existing.description ?? '')
      setVisibility(existing.visibility)
      setForApplication(existing.applicationId !== null)
      setInclude(Object.keys(existing.sections) as SectionId[])
    }
  }

  const effectiveVisibility: Visibility = updating ? existing.visibility : visibility
  const needsProject = effectiveVisibility !== 'organization' && !projectId
  const canSave = name.trim() !== '' && include.length > 0 && !needsProject && !pending

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSave) return
    const ordered = SECTION_IDS.filter(id => include.includes(id))
    const sections = sectionsFromDraft(draft, ordered)
    const applicationId = forApplication ? offeredApp : null
    const finish = (kind: 'team' | 'organization', id: string, version: number | null) => {
      onSaved({ kind, id, version, name: name.trim(), baseline: sections })
      onClose()
    }
    if (effectiveVisibility === 'organization') {
      const sectionDraft = applySections(parseDraftPayload({}), sections)
      const configurable = buildConfigurable(sectionDraft)
      if (!sections.systems) {
        for (const key of ['project_id', 'app_id', 'environment_id', 'test_scope']) delete configurable[key]
      }
      const assistantId = await saveOrganization.mutateAsync({
        assistantId: updating ? existing.id : null,
        name: name.trim(),
        description: description.trim() || null,
        applicationId,
        configurable,
      })
      finish('organization', assistantId, updating && existing.version !== null ? existing.version + 1 : 1)
      return
    }
    const body: TeamPresetWrite = {
      name: name.trim(),
      description: description.trim() || null,
      application_id: applicationId,
      visibility: effectiveVisibility,
      payload: { schema_version: 1, sections },
    }
    const saved = updating
      ? await updateTeam.mutateAsync({ id: existing.id, body: { ...body, expected_version: existing.version, note: note.trim() || null } })
      : await createTeam.mutateAsync({ ...body, project_id: projectId })
    finish('team', saved.id, saved.version)
  }

  const current = parseDraftPayload(draft)
  return (
    <Dialog overlayClassName="wizard-confirm-overlay" className="glass-panel preset-dialog" ariaLabel="Save as preset"
      onClose={() => { if (!pending) onClose() }} panelAs="form" onSubmit={(event) => void submit(event)}>
      <h2 className="preset-dialog-title">Save as preset</h2>
      <p className="preset-muted">Saves these settings so future tests can start from them. Nothing is launched.</p>
      {canUpdate && existing && (
        <fieldset className="preset-fieldset">
          <legend>Save to</legend>
          <label className="preset-choice"><input type="radio" name="preset-save-mode" checked={mode === 'update'} onChange={() => chooseMode('update')} />
            Update {existing.name}{existing.version ? ` (v${existing.version})` : ''}</label>
          <label className="preset-choice"><input type="radio" name="preset-save-mode" checked={mode === 'new'} onChange={() => chooseMode('new')} />
            Save as a new preset</label>
        </fieldset>
      )}
      <label className="preset-label">Name
        <input className="field-input" required value={name} onChange={event => setName(event.target.value)} maxLength={255} />
      </label>
      <label className="preset-label">Description
        <textarea className="field-input" rows={2} value={description} onChange={event => setDescription(event.target.value)} maxLength={2000} />
      </label>
      {!updating && (
        <fieldset className="preset-fieldset">
          <legend>Who can use it</legend>
          <label className="preset-choice"><input type="radio" name="preset-visibility" checked={visibility === 'personal'} onChange={() => setVisibility('personal')} disabled={!projectId} />
            Just me</label>
          <label className="preset-choice"><input type="radio" name="preset-visibility" checked={visibility === 'project'} onChange={() => setVisibility('project')} disabled={!canShareWithTeam} />
            {projectName ? `${projectName} team` : 'Project team'}
            {!canShareWithTeam && projectId && <span className="preset-muted"> (needs project-wide access)</span>}</label>
          {admin && <label className="preset-choice"><input type="radio" name="preset-visibility" checked={visibility === 'organization'} onChange={() => setVisibility('organization')} />
            Organization <span className="preset-muted">(every project; platform administrators maintain it)</span></label>}
        </fieldset>
      )}
      {offeredApp && (
        <label className="preset-choice">
          <input type="checkbox" checked={forApplication} onChange={event => setForApplication(event.target.checked)} />
          Offer it only for {names.applicationName(offeredApp)} tests <span className="preset-muted">(otherwise it's a General preset)</span>
        </label>
      )}
      {movedProject && (
        <p className="preset-warning" role="note">
          This test is now in {projectName}; the preset stays in {names.project(movedProject)}, where its team uses it.
          Save as a new preset to share it with {projectName} instead.
        </p>
      )}
      <fieldset className="preset-fieldset">
        <legend>Include</legend>
        {SECTION_IDS.map(id => {
          const summary = fieldsFor([id]).map(field => `${field.label}: ${field.format(current, names)}`).join(' · ')
          return (
            <label key={id} className="preset-choice preset-include">
              <input type="checkbox" checked={include.includes(id)}
                onChange={event => setInclude(previous => event.target.checked ? [...previous, id] : previous.filter(item => item !== id))} />
              <span><strong>{SECTION_LABELS[id]}</strong><span className="preset-include-summary">{summary}</span></span>
            </label>
          )
        })}
      </fieldset>
      {updating && (
        <label className="preset-label">What changed? <span className="preset-muted">(shown in the preset's history)</span>
          <input className="field-input" value={note} onChange={event => setNote(event.target.value)} maxLength={500} />
        </label>
      )}
      {needsProject && <p className="preset-warning" role="alert">Choose the primary system and its project first.</p>}
      {include.length === 0 && <p className="preset-warning" role="alert">Include at least one section.</p>}
      {failure && <p className="preset-warning" role="alert">Save failed: {failure.message}</p>}
      <div className="preset-dialog-actions">
        <button type="button" className="btn btn-secondary" onClick={onClose} disabled={pending}>Cancel</button>
        <button type="submit" className="btn btn-primary" disabled={!canSave}>{pending ? 'Saving…' : updating ? 'Update preset' : 'Save preset'}</button>
      </div>
    </Dialog>
  )
}
