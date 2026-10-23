/**
 * Shared form components for the environments screens (D5): the hosts row
 * editor used by both the list page's create panel and the detail page's
 * inline edit form, plus the kind chip. Pure helpers live in
 * environmentsLogic.ts (fast-refresh friendliness).
 */
import { useState } from 'react'

import type { HostDraft } from './environmentsLogic'

/** Add/remove rows of hostname + role inputs (create panel and edit form). */
export function HostsEditor({
  hosts,
  onChange,
}: {
  hosts: HostDraft[]
  onChange: (hosts: HostDraft[]) => void
}) {
  function patchRow(index: number, patch: Partial<HostDraft>) {
    onChange(hosts.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  }

  return (
    <div className="env-hosts-editor" role="group" aria-label="Hosts">
      {hosts.map((host, index) => (
        // Index keys are safe here: rows are fully controlled and only
        // appended/removed via the buttons below.
        <div className="env-hosts-row" key={index}>
          <input
            type="text"
            className="field-input"
            placeholder="hostname"
            aria-label={`Host ${index + 1} hostname`}
            value={host.hostname}
            onChange={(event) => patchRow(index, { hostname: event.target.value })}
          />
          <input
            type="text"
            className="field-input"
            placeholder="role (optional)"
            aria-label={`Host ${index + 1} role`}
            value={host.role}
            onChange={(event) => patchRow(index, { role: event.target.value })}
          />
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            aria-label={`Remove host ${index + 1}`}
            onClick={() => onChange(hosts.filter((_, i) => i !== index))}
          >
            Remove
          </button>
        </div>
      ))}
      <button
        type="button"
        className="btn btn-ghost btn-sm env-hosts-add"
        onClick={() => onChange([...hosts, { hostname: '', role: '' }])}
      >
        Add host
      </button>
    </div>
  )
}

/** Small kind chip ("k8s" / "vm" / …) shared by the list rows and detail header. */
export function KindChip({ kind }: { kind: string | null }) {
  if (!kind) return <span className="env-muted">—</span>
  return <span className="dash-context-chip env-kind-chip">{kind}</span>
}

/**
 * The projects an application is linked to. Known projects are checkboxes;
 * unscoped administrators can also type a new project ID. Projects the user
 * can't link or unlink stay as they are (disabled).
 */
export function ProjectsPicker({ value, onChange, options, canChange, allowOther, caption }: {
  value: string[]
  onChange: (next: string[]) => void
  options: string[]
  canChange: (projectId: string) => boolean
  allowOther: boolean
  caption: (projectId: string) => string
}) {
  const [other, setOther] = useState('')
  const all = [...new Set([...options, ...value])].sort()
  function toggle(projectId: string, checked: boolean) {
    onChange(checked ? [...value, projectId].sort() : value.filter(item => item !== projectId))
  }
  function addOther() {
    const projectId = other.trim()
    if (!projectId) return
    if (!value.includes(projectId)) onChange([...value, projectId].sort())
    setOther('')
  }
  return (
    <fieldset className="env-field env-projects-picker">
      <legend className="env-field-label">Projects</legend>
      <span className="env-field-help">Tests in these projects can choose this application and its environments.</span>
      {all.length > 0 && (
        <div className="env-projects-options">
          {all.map(projectId => (
            <label key={projectId} className="env-projects-option">
              <input type="checkbox" checked={value.includes(projectId)} disabled={!canChange(projectId)}
                onChange={(event) => toggle(projectId, event.target.checked)} />
              {caption(projectId)}
            </label>
          ))}
        </div>
      )}
      {allowOther && (
        <div className="env-projects-other">
          <input type="text" className="field-input" aria-label="Other project ID" placeholder="project-id" value={other}
            onChange={(event) => setOther(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); addOther() } }} />
          <button type="button" className="btn btn-ghost btn-sm" onClick={addOther} disabled={!other.trim()}>Add project</button>
        </div>
      )}
    </fieldset>
  )
}
