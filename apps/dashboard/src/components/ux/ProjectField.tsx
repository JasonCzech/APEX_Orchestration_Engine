import { useId } from 'react'
import { useOptionalConsumer } from '@/auth/AuthProvider'
import { useApplicationsIndex } from '@/api/hooks/useEnvironments'

/** The same authorized project choices in creation and resource workflows. */
export function ProjectField({ value, onChange, required = false, label = 'Project', ariaLabel, disabled = false }: {
  value: string; onChange: (value: string) => void; required?: boolean; label?: string; ariaLabel?: string; disabled?: boolean
}) {
  const id = useId()
  const consumer = useOptionalConsumer()
  const catalog = useApplicationsIndex()
  const scoped = Boolean(consumer?.scopes.length)
  const projects = [...new Set(scoped
    ? consumer!.scopes.map((scope) => scope.project_id)
    : (catalog.data ?? []).map((application) => application.project_id))].sort()
  return (
    <label className="ux-field">
      {label}
      {scoped ? <select aria-label={ariaLabel} disabled={disabled} value={value} required={required} onChange={(event) => onChange(event.target.value)}>
        <option value="">{required ? 'Select a project' : 'All authorized projects'}</option>
        {projects.map((project) => <option key={project} value={project}>{project}</option>)}
      </select> : <>
        <input aria-label={ariaLabel} disabled={disabled} list={id} value={value} required={required} onChange={(event) => onChange(event.target.value)} />
        <datalist id={id}>{projects.map((project) => <option key={project} value={project} />)}</datalist>
      </>}
    </label>
  )
}
