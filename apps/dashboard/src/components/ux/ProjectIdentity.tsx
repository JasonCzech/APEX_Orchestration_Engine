import { useEffect, useState } from 'react'
import { useOptionalConsumer } from '@/auth/AuthProvider'
import { useProjectLabels, useProjectLabelWrite } from '@/api/hooks/useProjectLabels'
import { Dialog } from '@/components/Dialog'
import { CopyableId } from './CopyableId'
import { useNavigationBlocker } from '@/components/useNavigationBlocker'
import './project-identity.css'

/** Names are authored metadata, never inferred from an application's name or ID. */
export function ProjectIdentity({ projectId, editable = false }: { projectId: string; editable?: boolean }) {
  const labels = useProjectLabels()
  const consumer = useOptionalConsumer()
  const name = !labels.isError ? labels.data?.find(item => item.project_id === projectId)?.name : undefined
  const [editing, setEditing] = useState(false)
  const canName = editable && consumer?.role === 'admin' && (!consumer.scopes.length || consumer.scopes.some(scope => scope.project_id === projectId && !scope.app_id))
  return <span className="project-identity">
    <span>{name ?? (labels.isPending ? 'Loading project name…' : 'Project name unavailable')}</span>{' '}
    <CopyableId value={projectId} label="Project ID" />
    {canName && <button className="btn btn-ghost btn-sm" onClick={() => setEditing(true)}>Edit project name</button>}
    {editing && <ProjectNameEditor projectId={projectId} initialName={name ?? ''} onClose={() => setEditing(false)} />}
  </span>
}

function ProjectNameEditor({ projectId, initialName, onClose }: { projectId: string; initialName: string; onClose: () => void }) {
  const [name, setName] = useState(initialName)
  const [remove, setRemove] = useState(false)
  const write = useProjectLabelWrite(projectId)
  const [discard, setDiscard] = useState(false)
  const dirty = name !== initialName || remove
  const blocker = useNavigationBlocker(dirty || write.isPending)
  useEffect(() => {
    if (!dirty && !write.isPending) return
    const unload = (event: BeforeUnloadEvent) => { event.preventDefault() }
    window.addEventListener('beforeunload', unload)
    return () => window.removeEventListener('beforeunload', unload)
  }, [dirty, write.isPending])
  const close = () => { if (write.isPending) return; if (dirty) setDiscard(true); else onClose() }
  return <>
    <Dialog className="project-name-dialog" overlayClassName="project-name-overlay" ariaLabel="Project display name" onClose={close}>
      <h2>Project display name</h2>
      <CopyableId value={projectId} label="Project ID" />
      <p>This name identifies the existing project. It does not change its ID, applications or permissions. Duplicate names are allowed; IDs distinguish them.</p>
      <label className="ux-field">Project name<input value={name} maxLength={255} disabled={write.isPending || remove} onChange={event => setName(event.target.value)} /></label>
      {initialName && <label><input type="checkbox" checked={remove} disabled={write.isPending} onChange={event => setRemove(event.target.checked)} />Remove this display name; keep the project and its resources</label>}
      {write.error && <p role="alert" className="tonal-card danger">{write.error.message}</p>}
      <div className="ux-dialog-actions"><button className="btn btn-secondary" disabled={write.isPending} onClick={close}>Cancel</button><button className="btn btn-primary" disabled={write.isPending || !dirty || (!remove && !name.trim())} onClick={() => write.mutate(remove ? null : name.trim(), { onSuccess: onClose })}>{write.isPending ? 'Saving…' : 'Save'}</button></div>
    </Dialog>
    {(discard || blocker.state === 'blocked') && <Dialog className="project-name-dialog" overlayClassName="project-name-overlay" ariaLabel="Discard project name changes" onClose={() => { setDiscard(false); blocker.reset?.() }}><h2>{write.isPending ? 'Project name is being saved' : 'Discard project name changes?'}</h2><p>{write.isPending ? 'Wait for this save to finish before leaving.' : 'Your unsaved name will be lost.'}</p><div className="ux-dialog-actions"><button className="btn btn-secondary" onClick={() => { setDiscard(false); blocker.reset?.() }}>Continue editing</button><button className="btn btn-danger" disabled={write.isPending} onClick={() => { if (blocker.state === 'blocked') blocker.proceed(); else onClose() }}>Discard changes</button></div></Dialog>}
  </>
}
