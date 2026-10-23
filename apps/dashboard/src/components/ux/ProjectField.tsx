import { useId } from 'react'
import { useOptionalConsumer } from '@/auth/AuthProvider'
import { useProjectLabels } from '@/api/hooks/useProjectLabels'
import { useApplicationsIndex } from '@/api/hooks/useEnvironments'

/** The same authorized project choices in creation and resource workflows. */
export function ProjectField({ value, onChange, required = false, label = 'Project', ariaLabel, disabled = false }: {
  value: string; onChange: (value: string) => void; required?: boolean; label?: string; ariaLabel?: string; disabled?: boolean
}) {
  const id = useId()
  const consumer = useOptionalConsumer()
  const catalog = useApplicationsIndex()
  const labels = useProjectLabels()
  const names = new Map((labels.isError ? [] : labels.data ?? []).map(row => [row.project_id, row.name]))
  const caption = (project: string) => `${names.get(project) ?? 'Project name unavailable'} (${project})`
  const scoped = Boolean(consumer?.scopes.length)
  const projects = [...new Set(scoped
    ? consumer!.scopes.map((scope) => scope.project_id)
    : (catalog.data ?? []).flatMap((application) => application.project_ids))].sort()
  return (
    <label className="ux-field">
      {label}
      {scoped ? <select aria-label={ariaLabel} disabled={disabled} value={value} required={required} onChange={(event) => onChange(event.target.value)}>
        <option value="">{required ? 'Select a project' : 'All authorized projects'}</option>
        {projects.map((project) => <option key={project} value={project}>{caption(project)}</option>)}
      </select> : <>
        <input aria-label={ariaLabel} disabled={disabled} list={id} value={value} required={required} onChange={(event) => onChange(event.target.value)} />
        <datalist id={id}>{projects.map((project) => <option key={project} value={project}>{caption(project)}</option>)}</datalist>
      </>}
    </label>
  )
}
