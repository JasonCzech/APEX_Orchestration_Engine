/**
 * The wizard's preset context (F-05, F-06, F-14): which preset this test started
 * from, how many of its values were changed since, and Reset / Detach / Change.
 * Changing presets shows exactly what would change before anything is replaced.
 */
import { useState } from 'react'

import { usePresets } from '@/api/hooks/useTestPresets'
import { Dialog } from '@/components/Dialog'
import { ChangeList } from '@/features/presets/PresetParts'
import { PresetPicker } from '@/features/presets/PresetPicker'
import {
  SECTION_IDS,
  applyPreset,
  changesSincePreset,
  detachPreset,
  fieldChanges,
  primaryApplication,
  resetToPreset,
  type Preset,
  type SectionId,
} from '@/features/presets/presetModel'
import { useFieldNames } from '@/features/presets/useFieldNames'

import type { DraftUpdater } from './useDraft'
import type { WizardDraft } from './wizardState'

type Pending = { kind: 'changes' } | { kind: 'reset' } | { kind: 'pick' } | { kind: 'confirm'; preset: Preset }

export function PresetBar({ draft, onChange, disabled }: {
  draft: WizardDraft
  onChange: (updater: DraftUpdater) => void
  disabled?: boolean
}) {
  const names = useFieldNames()
  const presets = usePresets()
  const [pending, setPending] = useState<Pending | null>(null)
  const link = draft.preset
  const changes = changesSincePreset(draft, names)
  const close = () => setPending(null)

  function confirmChange(preset: Preset) {
    const after = applyPreset(draft, preset)
    const removed = [
      draft.document_ids.length > after.document_ids.length && 'attached documents',
      draft.work_items.length > after.work_items.length && 'tickets',
      draft.context_summary_ids.length > after.context_summary_ids.length && 'context summaries',
    ].filter(Boolean) as string[]
    return { after, removed, changes: fieldChanges(draft, after, Object.keys(preset.sections) as SectionId[], names) }
  }

  return (
    <div className="glass-panel wizard-preset-bar" data-testid="preset-bar">
      {link ? (
        <>
          <span className="wizard-preset-chip">
            <span className="wizard-label">Preset</span>{' '}
            <strong>{link.name}</strong>
            {link.version ? <span className="wizard-caption"> · v{link.version}</span> : null}
            <span className="wizard-caption"> · {changes.length === 0 ? 'no changes' : `${changes.length} ${changes.length === 1 ? 'change' : 'changes'}`}</span>
          </span>
          <span className="wizard-preset-actions">
            <button type="button" className="btn btn-ghost btn-sm" disabled={changes.length === 0} onClick={() => setPending({ kind: 'changes' })}>View changes</button>
            <button type="button" className="btn btn-ghost btn-sm" disabled={disabled || changes.length === 0} onClick={() => setPending({ kind: 'reset' })}>Reset to preset</button>
            <button type="button" className="btn btn-ghost btn-sm" disabled={disabled} onClick={() => onChange(detachPreset)}
              title="Keep the current values and stop tracking the preset">Detach</button>
            <button type="button" className="btn btn-secondary btn-sm" disabled={disabled} onClick={() => setPending({ kind: 'pick' })}>Change preset…</button>
          </span>
        </>
      ) : (
        <>
          <span className="wizard-caption">Not started from a preset.</span>
          <button type="button" className="btn btn-secondary btn-sm" disabled={disabled} onClick={() => setPending({ kind: 'pick' })}>Use a preset…</button>
        </>
      )}

      {pending?.kind === 'changes' && link && (
        <Dialog overlayClassName="wizard-confirm-overlay" className="glass-panel preset-dialog" ariaLabel="Changes since the preset" onClose={close}>
          <h2 className="preset-dialog-title">Changes since {link.name}</h2>
          <ChangeList changes={changes} label="Changed settings" beforeLabel="Preset" afterLabel="This test" />
          <div className="preset-dialog-actions"><button type="button" className="btn btn-primary" onClick={close}>Close</button></div>
        </Dialog>
      )}
      {pending?.kind === 'reset' && link && (
        <Dialog overlayClassName="wizard-confirm-overlay" className="glass-panel preset-dialog" ariaLabel="Reset to preset" onClose={close}>
          <h2 className="preset-dialog-title">Reset to {link.name}?</h2>
          <p className="preset-muted">These settings go back to the preset's values. Everything else stays.</p>
          <ChangeList changes={changes.map(change => ({ ...change, before: change.after, after: change.before }))} label="Settings that reset" />
          <div className="preset-dialog-actions">
            <button type="button" className="btn btn-secondary" autoFocus onClick={close}>Cancel</button>
            <button type="button" className="btn btn-primary" onClick={() => { onChange(resetToPreset); close() }}>Reset</button>
          </div>
        </Dialog>
      )}
      {pending?.kind === 'pick' && (
        <Dialog overlayClassName="wizard-confirm-overlay" className="glass-panel preset-dialog preset-dialog--wide" ariaLabel="Choose a preset" onClose={close}>
          <h2 className="preset-dialog-title">{link ? 'Change preset' : 'Use a preset'}</h2>
          {presets.isError
            ? <p className="preset-warning" role="alert">Presets failed to load. <button type="button" className="btn btn-secondary btn-sm" onClick={presets.refetch}>Retry</button></p>
            : <PresetPicker presets={(presets.data ?? []).filter(preset => preset.key !== `${link?.kind}:${link?.id}`)} applicationId={primaryApplication(draft)} names={names}
                chooseLabel="Choose" onChoose={preset => setPending({ kind: 'confirm', preset })} />}
          <div className="preset-dialog-actions"><button type="button" className="btn btn-secondary" onClick={close}>Cancel</button></div>
        </Dialog>
      )}
      {pending?.kind === 'confirm' && (() => {
        const plan = confirmChange(pending.preset)
        return (
          <Dialog overlayClassName="wizard-confirm-overlay" className="glass-panel preset-dialog preset-dialog--wide" ariaLabel={`Apply ${pending.preset.name}`} onClose={close}>
            <h2 className="preset-dialog-title">Apply {pending.preset.name}?</h2>
            <ChangeList changes={plan.changes} label="Settings that change" />
            {plan.removed.length > 0 && <p className="preset-warning">It also removes {plan.removed.join(', ')}, because the systems under test change.</p>}
            <p className="preset-muted">Settings the preset doesn't include ({SECTION_IDS.filter(id => !(id in pending.preset.sections)).length} of {SECTION_IDS.length} sections) stay as they are.</p>
            <div className="preset-dialog-actions">
              <button type="button" className="btn btn-secondary" autoFocus onClick={() => setPending({ kind: 'pick' })}>Back</button>
              <button type="button" className="btn btn-primary" onClick={() => { onChange(previous => applyPreset(previous, pending.preset)); close() }}>Apply preset</button>
            </div>
          </Dialog>
        )
      })()}
    </div>
  )
}
