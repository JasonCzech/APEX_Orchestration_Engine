/**
 * One preset page for both kinds (F-07, F-08, F-13): its settings in the
 * wizard's own words, who uses it, its version history, and the actions —
 * start a test from it, edit it in the wizard, duplicate, archive, promote.
 */
import { useState, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router'

import {
  useDeleteTeamPreset,
  usePresetUsage,
  usePresetVersions,
  useSetTeamPresetArchived,
  type TestPresetVersionRead,
} from '@/api/hooks/useTestPresets'
import { useConsumer } from '@/auth/AuthProvider'
import { canLaunchPipelines, isGlobalAdmin } from '@/auth/RequireRole'
import { Dialog } from '@/components/Dialog'
import { formatRelative, formatTimestamp } from '@/utils/time'

import { ChangeList, PresetBadge, PresetFieldList } from './PresetParts'
import { SavePresetDialog } from './SavePresetDialog'
import { fieldChanges, presetDraft, presetHref, type Preset, type PresetSections, type SectionId } from './presetModel'
import { useFieldNames } from './useFieldNames'

function VersionHistory({ preset }: { preset: Preset }) {
  const versions = usePresetVersions(preset.id)
  const names = useFieldNames()
  if (versions.isPending) return <p className="preset-muted">Loading history…</p>
  if (versions.isError) return <p className="preset-warning" role="alert">History failed to load.</p>
  const rows = versions.data
  return (
    <ol className="preset-history" aria-label="Version history">
      {rows.map((row, index) => {
        const previous: TestPresetVersionRead | undefined = rows[index + 1]
        const sections = (row.payload as { sections?: PresetSections }).sections ?? {}
        const before = previous ? (previous.payload as { sections?: PresetSections }).sections ?? {} : null
        const ids = [...new Set([...Object.keys(sections), ...Object.keys(before ?? {})])] as SectionId[]
        const changes = before ? fieldChanges(presetDraft({ sections: before }), presetDraft({ sections }), ids, names) : []
        return (
          <li key={row.version} className="preset-history-entry">
            <p className="preset-history-head">
              <strong>v{row.version}</strong>
              {row.note && <> · {row.note}</>}
              <span className="preset-muted"> · {row.created_by ?? 'Unknown'}{row.created_at ? <>, <span title={formatTimestamp(row.created_at)}>{formatRelative(row.created_at)}</span></> : null}</span>
            </p>
            {previous && (
              <details>
                <summary>{changes.length === 0 ? 'No setting changes' : `${changes.length} setting ${changes.length === 1 ? 'change' : 'changes'}`}{row.name !== previous.name ? ` · renamed from ${previous.name}` : ''}</summary>
                <ChangeList changes={changes} label={`Changes in v${row.version}`} beforeLabel={`v${previous.version}`} afterLabel={`v${row.version}`} />
              </details>
            )}
          </li>
        )
      })}
    </ol>
  )
}

export function PresetDetail({ preset, advanced, extraActions }: {
  preset: Preset
  /** Kind-specific extras (organization: the raw configurable and its JSON editor). */
  advanced?: ReactNode
  extraActions?: ReactNode
}) {
  const navigate = useNavigate()
  const consumer = useConsumer()
  const names = useFieldNames()
  const usage = usePresetUsage([preset])
  const stats = usage.data?.get(preset.key)
  const archive = useSetTeamPresetArchived()
  const remove = useDeleteTeamPreset()
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [promoting, setPromoting] = useState(false)
  const canLaunch = canLaunchPipelines(consumer)
  const admin = isGlobalAdmin(consumer)
  const startHref = `/runs/new?preset=${encodeURIComponent(preset.key)}`

  return (
    <section className="gc-page preset-page animate-enter">
      <Link to="/test-presets">All test presets</Link>
      <header className="glass-panel preset-detail-header">
        <div className="preset-detail-title">
          <h2 className="gc-page-title">{preset.name}</h2>
          <PresetBadge preset={preset} />
        </div>
        <p className="preset-detail-facts" data-testid="preset-facts">
          {preset.applicationId ? <>For <strong>{names.applicationName(preset.applicationId)}</strong></> : <>General: any application</>}
          {preset.projectId && <> · {names.project(preset.projectId)}</>}
          {preset.version !== null && <> · v{preset.version}</>}
          {preset.updatedAt && <> · updated <span title={formatTimestamp(preset.updatedAt)}>{formatRelative(preset.updatedAt)}</span>{preset.updatedBy ? ` by ${preset.updatedBy}` : ''}</>}
          {preset.owner && preset.visibility !== 'organization' && <> · created by {preset.owner}</>}
        </p>
        {preset.description && <p className="gc-card-description">{preset.description}</p>}
        <div className="gc-actions-row">
          {canLaunch && !preset.archived && <Link className="btn btn-primary btn-sm" to={startHref}>Start a test from this preset</Link>}
          {canLaunch && preset.editable && !preset.archived && (
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => void navigate(`${startHref}&step=review`)}
              title="Opens the wizard with this preset; on Review choose Save as preset → Update">Edit in the wizard</button>
          )}
          {canLaunch && (
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => void navigate(`${startHref}&step=review`)}
              title="Opens the wizard with this preset; on Review choose Save as preset → Save as a new preset">Duplicate</button>
          )}
          {preset.kind === 'team' && preset.editable && (
            <button type="button" className="btn btn-ghost btn-sm" disabled={archive.isPending}
              onClick={() => archive.mutate({ id: preset.id, archived: !preset.archived })}>
              {preset.archived ? 'Unarchive' : 'Archive'}
            </button>
          )}
          {preset.kind === 'team' && preset.editable && (
            <button type="button" className="btn btn-ghost btn-sm env-danger-text" onClick={() => setConfirmDelete(true)}>Delete…</button>
          )}
          {preset.kind === 'team' && admin && (
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setPromoting(true)}>Promote to organization…</button>
          )}
          {extraActions}
        </div>
        {(archive.error ?? remove.error) && <p className="preset-warning" role="alert">{(archive.error ?? remove.error)!.message}</p>}
      </header>

      <div className="preset-detail-body">
        <section className="glass-panel preset-detail-section" aria-labelledby="preset-settings-heading">
          <h3 id="preset-settings-heading" className="gc-section-title">Settings</h3>
          <p className="preset-muted">What this preset fills in when a test starts from it. Anything it doesn't include stays as the person sets it.</p>
          <PresetFieldList preset={preset} names={names} />
        </section>
        <section className="glass-panel preset-detail-section" aria-labelledby="preset-usage-heading">
          <h3 id="preset-usage-heading" className="gc-section-title">Usage</h3>
          {usage.isPending ? <p className="preset-muted">Loading…</p>
            : usage.isError ? <p className="preset-muted">Usage couldn't be loaded.</p>
            : stats ? <p>Started {stats.tests} {stats.tests === 1 ? 'test' : 'tests'} you can see{stats.lastUsedAt ? <>, most recently <span title={formatTimestamp(stats.lastUsedAt)}>{formatRelative(stats.lastUsedAt)}</span></> : null}.</p>
            : <p className="preset-muted">No tests you can see have started from this preset yet.</p>}
        </section>
        {preset.kind === 'team' && (
          <section className="glass-panel preset-detail-section preset-detail-section--wide" aria-labelledby="preset-history-heading">
            <h3 id="preset-history-heading" className="gc-section-title">History</h3>
            <VersionHistory preset={preset} />
          </section>
        )}
        {advanced}
      </div>

      {confirmDelete && (
        <Dialog overlayClassName="wizard-confirm-overlay" className="glass-panel preset-dialog" ariaLabel={`Delete preset ${preset.name}`} onClose={() => setConfirmDelete(false)}>
          <h2 className="preset-dialog-title">Delete {preset.name}?</h2>
          <p className="preset-muted">Its history goes with it. Tests that started from it keep their settings and still name it. Archive it instead to hide it from new tests but keep it.</p>
          <div className="preset-dialog-actions">
            <button type="button" className="btn btn-secondary" autoFocus onClick={() => setConfirmDelete(false)}>Cancel</button>
            <button type="button" className="btn btn-danger" disabled={remove.isPending}
              onClick={() => remove.mutate(preset.id, { onSuccess: () => void navigate('/test-presets') })}>Delete preset</button>
          </div>
        </Dialog>
      )}
      {promoting && (
        <SavePresetDialog draft={presetDraft(preset)} initialName={preset.name} initialDescription={preset.description}
          initialSections={Object.keys(preset.sections) as SectionId[]}
          onClose={() => setPromoting(false)} onSaved={link => void navigate(presetHref(link.kind, link.id))} />
      )}
    </section>
  )
}
