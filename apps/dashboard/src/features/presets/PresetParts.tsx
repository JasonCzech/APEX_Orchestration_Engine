/** Small preset building blocks shared by the wizard, the save dialog and the presets pages. */
import { fieldsFor, presetDraft, type FieldChange, type FieldNames, type Preset, type PresetVisibility } from './presetModel'
import './presets.css'

const VISIBILITY_LABELS: Record<PresetVisibility, string> = {
  personal: 'Just me',
  project: 'Team',
  organization: 'Organization',
}

export function PresetBadge({ preset }: { preset: Pick<Preset, 'visibility' | 'archived'> }) {
  return (
    <span className="preset-badges">
      <span className={`status-badge ${preset.visibility === 'organization' ? 'info' : preset.visibility === 'personal' ? 'neutral' : 'success'} preset-badge`}>
        {VISIBILITY_LABELS[preset.visibility]}
      </span>
      {preset.archived && <span className="status-badge neutral preset-badge">Archived</span>}
    </span>
  )
}

/** Every field a preset sets, in the wizard's words. */
export function PresetFieldList({ preset, names, label = 'Preset settings' }: {
  preset: Pick<Preset, 'sections'>
  names: FieldNames
  label?: string
}) {
  const draft = presetDraft(preset)
  const fields = fieldsFor(preset.sections)
  if (fields.length === 0) return <p className="preset-muted">This preset sets nothing.</p>
  return (
    <dl className="preset-fields" aria-label={label}>
      {fields.map(field => (
        <div key={field.id} className="preset-field">
          <dt>{field.label}</dt>
          <dd>{field.format(draft, names)}</dd>
        </div>
      ))}
    </dl>
  )
}

/** Before → after rows for a set of field changes. */
export function ChangeList({ changes, label, beforeLabel = 'Now', afterLabel = 'After' }: {
  changes: FieldChange[]
  label: string
  beforeLabel?: string
  afterLabel?: string
}) {
  if (changes.length === 0) return <p className="preset-muted">Nothing changes.</p>
  return (
    <table className="data-table preset-changes" aria-label={label}>
      <thead><tr><th>Setting</th><th>{beforeLabel}</th><th>{afterLabel}</th></tr></thead>
      <tbody>
        {changes.map(change => (
          <tr key={change.field.id}>
            <th scope="row">{change.field.label}</th>
            <td>{change.before}</td>
            <td>{change.after}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
