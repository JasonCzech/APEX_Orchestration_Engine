/**
 * /test-presets/:assistantId — an organization preset (a `pipeline` assistant):
 * the shared preset page (settings in the wizard's words, usage, actions) plus,
 * for platform administrators, the raw configurable and its JSON editor.
 * Saving publishes a new assistant version.
 */
import { useState } from 'react'
import { useParams } from 'react-router'

import { PHASE_NAMES } from '@apex/pipeline-events'

import {
  goldenConfigWriteMutationKey,
  useGoldenConfig,
  useUpdateGoldenConfig,
  type GoldenConfigEntry,
} from '@/api/hooks/useAssistants'
import { usePendingMutationCount } from '@/api/hooks/usePendingMutationCount'
import { organizationPreset } from '@/api/hooks/useTestPresets'
import { useConsumer } from '@/auth/AuthProvider'
import { isGlobalAdmin } from '@/auth/RequireRole'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { ProblemCard } from '@/components/ProblemCard'
import { Skeleton } from '@/components/ux/Skeleton'
import { PresetDetail } from '@/features/presets/PresetDetail'
import { useTimestampPreference } from '@/theme/preferences'

import {
  engineLabel,
  engineOf,
  gateMatrixView,
  parseConfigurableJson,
  phaseLabel,
  promptPinsView,
} from './configView'
import './golden-configs.css'

function EditPanel({
  entry,
  onClose,
}: {
  entry: GoldenConfigEntry
  onClose: (assistantId: string) => void
}) {
  const update = useUpdateGoldenConfig(entry.assistantId)
  const writeCount = usePendingMutationCount(goldenConfigWriteMutationKey(entry.assistantId))
  const writePending = writeCount > 0
  const [text, setText] = useState(() => JSON.stringify(entry.configurable, null, 2))
  const parse = parseConfigurableJson(text)
  const canSave = parse.ok && !writePending

  function patchConfig(patch: Record<string, unknown>) {
    if (parse.ok) setText(JSON.stringify({ ...parse.value, ...patch }, null, 2))
  }

  function save() {
    if (!parse.ok || writePending) return
    update.mutate(
      { configurable: parse.value },
      { onSuccess: () => onClose(entry.assistantId) },
    )
  }

  return (
    <section className="glass-panel gc-section gc-section--wide" aria-label="Edit configurable">
      <h3 className="gc-section-title">Edit configurable</h3>
      <p className="gc-muted">
        The full config.configurable bundle, replaced on save. Saving publishes a new assistant
        version.
      </p>
      {parse.ok && <fieldset disabled={writePending}>
        <legend>Preset settings</legend>
        <label>Engine<select className="field-select" aria-label="Preset engine" value={engineOf(parse.value)} onChange={(event) => patchConfig({ engine: event.target.value })}>
          {['sim', 'apex_load', 'loadrunner'].map((engine) => <option key={engine} value={engine}>{engineLabel(engine)}</option>)}
          {!['sim', 'apex_load', 'loadrunner'].includes(engineOf(parse.value)) && <option value={engineOf(parse.value)}>{engineLabel(engineOf(parse.value))}</option>}
        </select></label>
        <table className="data-table"><caption>Review policy</caption><thead><tr><th>Phase</th><th>Prompt review</th><th>Output review</th></tr></thead><tbody>{PHASE_NAMES.map((phase) => <tr key={phase}><th>{phaseLabel(phase)}</th>{(['prompt_review', 'output_review'] as const).map((gate) => <td key={gate}><select className="field-select" aria-label={`${phaseLabel(phase)} ${gate.replace('_', ' ')}`} value={gateMatrixView(parse.value['gates'])[phase][gate]} onChange={(event) => { const matrix = gateMatrixView(parse.value['gates']); patchConfig({ gates: { ...matrix, [phase]: { ...matrix[phase], [gate]: event.target.value } } }) }}><option value="gated">Review required</option><option value="auto">Automatic</option></select></td>)}</tr>)}</tbody></table>
        {promptPinsView(parse.value).map((pin) => <label key={pin.key}>{pin.key}<textarea className="field-input" aria-label={`Prompt pin ${pin.key}`} value={pin.detail ?? ''} onChange={(event) => { const overrides = parse.value['prompt_overrides'] as Record<string, unknown>; patchConfig({ prompt_overrides: { ...overrides, [pin.key]: { [pin.kind === 'version' ? 'version_id' : 'content']: event.target.value } } }) }} /></label>)}
      </fieldset>}
      <details><summary>Advanced JSON</summary>
      <textarea
        className="field-input gc-json-editor"
        aria-label="Configurable JSON"
        rows={16}
        spellCheck={false}
        value={text}
        disabled={writePending}
        onChange={(event) => setText(event.target.value)}
      />
      </details>
      {!parse.ok && (
        <p className="gc-form-error" role="alert">
          {parse.message}
        </p>
      )}
      {update.isError && (
        <p className="gc-form-error" role="alert">
          Update failed: {update.error.message}
        </p>
      )}
      <div className="gc-actions-row">
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => onClose(entry.assistantId)}
          disabled={writePending}
        >
          Cancel
        </button>
        <button type="button" className="btn btn-primary btn-sm" disabled={!canSave} onClick={save}>
          {writePending ? 'Saving…' : 'Save new version'}
        </button>
      </div>
    </section>
  )
}

function GoldenConfigDetail({ assistantId }: { assistantId: string }) {
  const consumer = useConsumer()
  const canEdit = isGlobalAdmin(consumer)
  const query = useGoldenConfig(assistantId)
  const writeCount = usePendingMutationCount(goldenConfigWriteMutationKey(assistantId))
  const writePending = writeCount > 0
  const [editing, setEditing] = useState(false)

  if (query.isPending) {
    return (
      <Skeleton label="Loading test preset" rows={2} />
    )
  }

  if (!query.data) {
    return (
      <ProblemCard
        title="Test preset unavailable"
        message="This test preset could not be loaded. Try again, or check the system status if the problem continues."
        onRetry={() => void query.refetch()}
      />
    )
  }

  const entry = query.data
  const preset = organizationPreset(entry)

  return (
    <>
      {query.isError && <CachedDataWarning error={query.error} onRetry={() => void query.refetch()} />}
      <PresetDetail
        preset={preset}
        extraActions={<>
          {writePending && <span className="topbar-meta-chip warning">saving…</span>}
          {entry.isSystemDefault && <span className="topbar-meta-chip info" data-testid="gc-system-chip">system default</span>}
          {canEdit && !editing && (
            <button type="button" className="btn btn-ghost btn-sm" disabled={writePending} onClick={() => setEditing(true)}>
              Edit JSON (advanced)
            </button>
          )}
          {!canEdit && <span className="gc-muted">Organization presets are maintained by platform administrators.</span>}
        </>}
        advanced={editing
          ? <EditPanel entry={entry} onClose={() => setEditing(false)} />
          : (
            <details className="glass-panel preset-detail-section preset-detail-section--wide gc-raw" data-testid="gc-raw-json">
              <summary>Raw configurable JSON</summary>
              <pre className="gc-raw-pre">{JSON.stringify(entry.configurable, null, 2)}</pre>
            </details>
          )}
      />
    </>
  )
}

export function GoldenConfigDetailPage() {
  useTimestampPreference()
  const { assistantId = '' } = useParams()
  // Reset edit state and resource-bound hooks whenever the route identity changes.
  return <GoldenConfigDetail key={assistantId} assistantId={assistantId} />
}
