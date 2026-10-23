/**
 * Confirmation and edit dialogs shared by the environments list and detail
 * pages: delete environment, edit / archive / delete application.
 */
import { useState, type FormEvent } from 'react'

import {
  environmentWriteMutationKey,
  useDeleteApplication,
  useDeleteEnvironment,
  useSetApplicationArchived,
  useUpdateApplication,
  type Application,
  type Environment,
} from '@/api/hooks/useEnvironments'
import { usePendingMutationCount } from '@/api/hooks/usePendingMutationCount'
import { usePromptList } from '@/api/hooks/usePrompts'
import { Dialog } from '@/components/Dialog'

import { ProjectsPicker } from './environmentsForm'
import type { ProjectChoices } from './environmentsLogic'

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`
}

/** Type-to-confirm delete for an environment; says what still refers to it. */
export function DeleteEnvironmentDialog({ environment, onClose, onDeleted }: {
  environment: Environment
  onClose: () => void
  onDeleted?: () => void
}) {
  const remove = useDeleteEnvironment(environment.id)
  const writeCount = usePendingMutationCount(environmentWriteMutationKey(environment.id))
  // Environment-context prompts tied to this environment (Prompts catalog).
  const prompts = usePromptList({ namespace: 'environment', environment_id: environment.id, includeArchived: true })
  const [confirmation, setConfirmation] = useState('')
  const canDelete = confirmation === environment.name && writeCount === 0
  const contextCount = prompts.data?.length ?? 0

  function close() {
    if (remove.isPending) return
    onClose()
  }

  return (
    <Dialog overlayClassName="env-overlay" className="env-modal glass-panel" ariaLabel={`Delete environment ${environment.name}`} onClose={close}>
      <h2 className="env-panel-title">Delete environment</h2>
      <p className="env-modal-caption">
        This permanently removes <strong>{environment.name}</strong>, its hosts and its scan history. Type the environment name to confirm.
      </p>
      <ul className="env-modal-list">
        {contextCount > 0
          ? <li>{plural(contextCount, 'environment-context prompt')} (archived included) {contextCount === 1 ? 'is' : 'are'} tied to it. The delete is refused until {contextCount === 1 ? 'it is' : 'they are'} moved or deleted in the Prompts catalog.</li>
          : <li>Prompts tied to this environment block the delete; none are tied to it now.</li>}
        <li>Tests and test presets that used it keep its old identifier and no longer resolve to an environment.</li>
      </ul>
      <input type="text" className="field-input" aria-label="Type the environment name to confirm" placeholder={environment.name}
        value={confirmation} onChange={(event) => setConfirmation(event.target.value)} />
      {remove.isError && <div className="env-inline-error" role="alert"><span>Delete failed: {remove.error.message}</span></div>}
      <div className="env-panel-actions">
        <button type="button" className="btn btn-ghost btn-sm" onClick={close} disabled={remove.isPending}>Cancel</button>
        <button type="button" className="btn btn-danger btn-sm" disabled={!canDelete}
          onClick={() => remove.mutate(environment.id, { onSuccess: () => { onClose(); onDeleted?.() } })}>
          {remove.isPending ? 'Deleting…' : 'Delete environment'}
        </button>
      </div>
    </Dialog>
  )
}

/** Rename, describe, or link/unlink projects. Names are unique across the catalog. */
export function EditApplicationDialog({ application, siblingNames, projects, onClose }: {
  application: Application
  siblingNames: string[]
  projects: ProjectChoices
  onClose: () => void
}) {
  const update = useUpdateApplication()
  const [name, setName] = useState(application.name)
  const [description, setDescription] = useState(application.description ?? '')
  const [projectIds, setProjectIds] = useState(application.project_ids)
  const trimmed = name.trim()
  const duplicate = trimmed !== application.name && siblingNames.includes(trimmed)
  const projectsChanged = projectIds.join('\n') !== application.project_ids.join('\n')
  const unlinked = application.project_ids.filter(projectId => !projectIds.includes(projectId))
  const changed = trimmed !== application.name || (description.trim() || null) !== (application.description ?? null) || projectsChanged
  const canSave = trimmed !== '' && !duplicate && projectIds.length > 0 && changed && !update.isPending

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSave) return
    const body: { name?: string; description?: string | null; project_ids?: string[] } = {}
    if (trimmed !== application.name) body.name = trimmed
    if ((description.trim() || null) !== (application.description ?? null)) body.description = description.trim() || null
    if (projectsChanged) body.project_ids = projectIds
    update.mutate({ applicationId: application.id, body }, { onSuccess: onClose })
  }

  return (
    <Dialog overlayClassName="env-overlay" className="env-modal glass-panel" ariaLabel={`Edit application ${application.name}`}
      onClose={() => { if (!update.isPending) onClose() }} panelAs="form" onSubmit={submit}>
      <h2 className="env-panel-title">Edit application</h2>
      <label className="env-field">
        <span className="env-field-label">Name</span>
        <input type="text" className="field-input" aria-label="Application name" value={name} onChange={(event) => setName(event.target.value)}
          aria-invalid={duplicate || undefined} aria-describedby={duplicate ? 'edit-application-duplicate' : undefined} />
      </label>
      {duplicate && <p id="edit-application-duplicate" className="env-form-error" role="alert">Another application already uses this name.</p>}
      <label className="env-field">
        <span className="env-field-label">Description</span>
        <textarea className="field-input" aria-label="Application description" rows={3} value={description} onChange={(event) => setDescription(event.target.value)} />
      </label>
      <ProjectsPicker value={projectIds} onChange={setProjectIds} {...projects} />
      {projectIds.length === 0 && <p className="env-form-error" role="alert">Keep at least one project.</p>}
      {unlinked.length > 0 && (
        <p className="env-field-help env-target-note" role="status">
          New tests in {unlinked.map(projects.caption).join(', ')} can no longer choose {application.name} or its environments. Existing tests keep their history.
        </p>
      )}
      {update.isError && <div className="env-inline-error" role="alert"><span>Save failed: {update.error.message}</span></div>}
      <div className="env-panel-actions">
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClose} disabled={update.isPending}>Cancel</button>
        <button type="submit" className="btn btn-primary btn-sm" disabled={!canSave}>{update.isPending ? 'Saving…' : 'Save changes'}</button>
      </div>
    </Dialog>
  )
}

/** Archive hides an application (and its environments) from new tests; unarchive restores it. */
export function ArchiveApplicationDialog({ application, environmentCount, onClose }: {
  application: Application
  environmentCount: number
  onClose: () => void
}) {
  const setArchived = useSetApplicationArchived()
  const archiving = !application.archived_at
  return (
    <Dialog overlayClassName="env-overlay" className="env-modal glass-panel" ariaLabel={`${archiving ? 'Archive' : 'Unarchive'} application ${application.name}`}
      onClose={() => { if (!setArchived.isPending) onClose() }}>
      <h2 className="env-panel-title">{archiving ? 'Archive application' : 'Unarchive application'}</h2>
      <p className="env-modal-caption">
        {archiving
          ? <>New tests can no longer choose <strong>{application.name}</strong> or its {plural(environmentCount, 'environment')}. Existing tests and their history are kept, and you can unarchive it later.</>
          : <><strong>{application.name}</strong> and its {plural(environmentCount, 'environment')} become available to new tests again.</>}
      </p>
      {setArchived.isError && <div className="env-inline-error" role="alert"><span>{setArchived.error.message}</span></div>}
      <div className="env-panel-actions">
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClose} disabled={setArchived.isPending}>Cancel</button>
        <button type="button" className="btn btn-primary btn-sm" disabled={setArchived.isPending}
          onClick={() => setArchived.mutate({ applicationId: application.id, archived: archiving }, { onSuccess: onClose })}>
          {setArchived.isPending ? 'Saving…' : archiving ? 'Archive application' : 'Unarchive application'}
        </button>
      </div>
    </Dialog>
  )
}

/** Type-to-confirm delete for an application and all its environments (administrators with access to it). */
export function DeleteApplicationDialog({ application, environmentCount, onClose }: {
  application: Application
  environmentCount: number
  onClose: () => void
}) {
  const remove = useDeleteApplication()
  const [confirmation, setConfirmation] = useState('')
  return (
    <Dialog overlayClassName="env-overlay" className="env-modal glass-panel" ariaLabel={`Delete application ${application.name}`}
      onClose={() => { if (!remove.isPending) onClose() }}>
      <h2 className="env-panel-title">Delete application</h2>
      <p className="env-modal-caption">
        This permanently removes <strong>{application.name}</strong> and its {plural(environmentCount, 'environment')}. If prompts are still tied to it or its environments, the server refuses and you can archive it instead. Tests and test presets that used it keep the old identifiers. Type the application name to confirm.
      </p>
      <input type="text" className="field-input" aria-label="Type the application name to confirm" placeholder={application.name}
        value={confirmation} onChange={(event) => setConfirmation(event.target.value)} />
      {remove.isError && <div className="env-inline-error" role="alert"><span>Delete failed: {remove.error.message}</span></div>}
      <div className="env-panel-actions">
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClose} disabled={remove.isPending}>Cancel</button>
        <button type="button" className="btn btn-danger btn-sm" disabled={confirmation !== application.name || remove.isPending}
          onClick={() => remove.mutate(application.id, { onSuccess: onClose })}>
          {remove.isPending ? 'Deleting…' : 'Delete application'}
        </button>
      </div>
    </Dialog>
  )
}
