/**
 * Shared form components for the environments screens (D5): the hosts row
 * editor used by both the list page's create panel and the detail page's
 * inline edit form, plus the kind chip. Pure helpers live in
 * environmentsLogic.ts (fast-refresh friendliness).
 */
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
