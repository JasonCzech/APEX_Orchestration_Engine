/**
 * Compact, searchable preset list (F-15/F-16). Presets for the primary
 * application come first, then General presets (no application). Presets for
 * other applications stay behind a toggle, because applying one changes the
 * systems under test.
 */
import { useId, useState } from 'react'

import { PresetBadge, PresetFieldList } from './PresetParts'
import { presetSummary, type FieldNames, type Preset } from './presetModel'

const ORDER: Record<string, number> = { personal: 0, project: 1, organization: 2 }

function sortPresets(presets: Preset[]): Preset[] {
  return [...presets].sort((a, b) =>
    Number(b.mine) - Number(a.mine) || ORDER[a.visibility]! - ORDER[b.visibility]! || a.name.localeCompare(b.name))
}

function PresetRow({ preset, names, other, chooseLabel, onChoose }: {
  preset: Preset
  names: FieldNames & { applicationName: (id: string | null) => string }
  other: boolean
  chooseLabel: string
  onChoose: (preset: Preset) => void
}) {
  const [open, setOpen] = useState(false)
  const previewId = useId()
  const summary = presetSummary(preset, names)
  return (
    <li className="preset-row">
      <div className="preset-row-main">
        <span className="preset-row-name">{preset.name}</span>
        <PresetBadge preset={preset} />
        {other && <span className="preset-muted">For {names.applicationName(preset.applicationId)}</span>}
      </div>
      {preset.description && <p className="preset-row-description">{preset.description}</p>}
      {summary.length > 0 && <p className="preset-row-summary">{summary.join(' · ')}</p>}
      <div className="preset-row-actions">
        <button type="button" className="btn btn-ghost btn-sm" aria-expanded={open} aria-controls={previewId} onClick={() => setOpen(value => !value)}>
          {open ? 'Hide preview' : 'Preview'}
        </button>
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => onChoose(preset)} aria-label={`${chooseLabel}: ${preset.name}`}>
          {chooseLabel}
        </button>
      </div>
      {open && (
        <div id={previewId} className="preset-row-preview">
          {other && <p className="preset-warning">Applying it changes the systems under test to this preset's.</p>}
          <PresetFieldList preset={preset} names={names} label={`${preset.name} settings`} />
        </div>
      )}
    </li>
  )
}

export function PresetPicker({ presets, applicationId, names, onChoose, chooseLabel = 'Use preset', emptyHint }: {
  presets: Preset[]
  /** The primary application under test; null lists presets for every application. */
  applicationId: string | null
  names: FieldNames & { applicationName: (id: string | null) => string }
  onChoose: (preset: Preset) => void
  chooseLabel?: string
  emptyHint?: string
}) {
  const [query, setQuery] = useState('')
  const [showOther, setShowOther] = useState(false)
  const needle = query.trim().toLowerCase()
  const visible = presets.filter(preset => !preset.archived && (!needle ||
    `${preset.name} ${preset.description ?? ''} ${names.applicationName(preset.applicationId)}`.toLowerCase().includes(needle)))
  const forApp = applicationId ? sortPresets(visible.filter(p => p.applicationId === applicationId)) : []
  const general = sortPresets(visible.filter(p => p.applicationId === null))
  const other = sortPresets(visible.filter(p => p.applicationId !== null && p.applicationId !== applicationId))
  const groups: { id: string; title: string; presets: Preset[]; other: boolean }[] = applicationId
    ? [
        { id: 'app', title: `For ${names.applicationName(applicationId)}`, presets: forApp, other: false },
        { id: 'general', title: 'General (any application)', presets: general, other: false },
        ...(showOther ? [{ id: 'other', title: 'Other applications', presets: other, other: true }] : []),
      ]
    : [
        { id: 'general', title: 'General (any application)', presets: general, other: false },
        ...[...new Set(other.map(p => p.applicationId!))]
          .sort((a, b) => names.applicationName(a).localeCompare(names.applicationName(b)))
          .map(app => ({ id: `app-${app}`, title: `For ${names.applicationName(app)}`, presets: other.filter(p => p.applicationId === app), other: false })),
      ]
  const shown = groups.filter(group => group.presets.length > 0)
  return (
    <div className="preset-picker">
      <label className="ux-field preset-search">Search presets
        <input type="search" className="field-input" value={query} onChange={event => setQuery(event.target.value)} placeholder="Name or description" />
      </label>
      {shown.length === 0 && <p className="preset-muted">{needle ? 'No presets match this search.' : emptyHint ?? 'No presets for this application yet.'}</p>}
      {shown.map(group => (
        <section key={group.id} className="preset-group" aria-label={group.title}>
          <h3 className="preset-group-title">{group.title} <span className="preset-muted">{group.presets.length}</span></h3>
          <ul className="preset-list">
            {group.presets.map(preset => (
              <PresetRow key={preset.key} preset={preset} names={names} other={group.other} chooseLabel={chooseLabel} onChoose={onChoose} />
            ))}
          </ul>
        </section>
      ))}
      {applicationId && other.length > 0 && (
        <button type="button" className="btn btn-ghost btn-sm" aria-expanded={showOther} onClick={() => setShowOther(value => !value)}>
          {showOther ? 'Hide presets for other applications' : `Show presets for other applications (${other.length})`}
        </button>
      )}
    </div>
  )
}
