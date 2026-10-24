/** A test's preset: where it started from (F-05) and saving its setup as a preset (F-02). */
import { useState } from 'react'
import { Link } from 'react-router'

import { useRunPreset } from '@/api/hooks/useTestPresets'
import { SavePresetDialog } from '@/features/presets/SavePresetDialog'
import type { WizardDraft } from '@/features/new-test/wizardState'
import { presetHref } from '@/features/presets/presetModel'

import { loadRunSetupDraft } from './runSetup'

/** "Started from preset <name> · v4 · modified", or nothing. */
export function RunPresetLine({ threadId }: { threadId: string }) {
  const link = useRunPreset(threadId)
  if (!link.data) return null
  const { kind, preset_id: id, name, version, modified } = link.data
  return (
    <p className="run-preset-line" data-testid="run-preset-line">
      Started from preset <Link to={presetHref(kind, id)}>{name || id}</Link>
      {version ? ` · v${version}` : ''}{modified ? ' · modified before launch' : ''}
    </p>
  )
}

export function SaveRunAsPresetButton({ threadId }: { threadId: string }) {
  const [draft, setDraft] = useState<WizardDraft | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<{ kind: string; id: string; name: string } | null>(null)
  async function open() {
    setLoading(true)
    setError(null)
    try {
      const setup = await loadRunSetupDraft(threadId)
      // The copy suffix belongs to new tests, not to a preset's default name.
      setDraft({ ...setup, title: setup.title.replace(/ \(copy(?: \d+)?\)$/, '') })
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Could not load this test setup.')
    } finally {
      setLoading(false)
    }
  }
  return (
    <span>
      <button type="button" className="btn btn-secondary btn-sm" disabled={loading} onClick={() => void open()}>
        {loading ? 'Loading setup…' : 'Save setup as preset…'}
      </button>
      {error && <span role="alert">{error}</span>}
      {saved && <span role="status"> Saved as <Link to={presetHref(saved.kind, saved.id)}>{saved.name}</Link>.</span>}
      {draft && (
        <SavePresetDialog draft={draft} onClose={() => setDraft(null)}
          onSaved={link => setSaved({ kind: link.kind, id: link.id, name: link.name })} />
      )}
    </span>
  )
}
