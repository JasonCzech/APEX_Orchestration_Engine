import { useTimestampPreference } from '@/theme/preferences'
import { Skeleton } from '@/components/ux/Skeleton'
import { ProjectIdentity } from '@/components/ux/ProjectIdentity'
/**
 * /golden-configs/:assistantId — structured READ view of the assistant's
 * pinned config.configurable (scope defaults, engine, gate policy matrix,
 * prompt pins, limits) + collapsible raw JSON.
 *
 * Editing: SDK assistants.update IS browser-exposed (verified against
 * @langchain/langgraph-sdk client typings), so admins get an [Edit JSON]
 * mode — parse-validated textarea, saved via useUpdateGoldenConfig; the server
 * bumps the assistant version on every update.
 *
 * [New test from this preset] deep-links the wizard's Config step with
 * ?golden=<assistant_id>; the wizard preselects the matching golden config.
 */
import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'

import { PHASE_NAMES } from '@apex/pipeline-events'

import {
  goldenConfigWriteMutationKey,
  useGoldenConfig,
  useUpdateGoldenConfig,
  type GoldenConfigEntry,
} from '@/api/hooks/useAssistants'
import { usePendingMutationCount } from '@/api/hooks/usePendingMutationCount'
import { useConsumer } from '@/auth/AuthProvider'
import { isGlobalAdmin } from '@/auth/RequireRole'
import { canLaunchPipelines } from '@/auth/RequireRole'
import { useApplications, useEnvironments } from '@/api/hooks/useCatalog'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { ProblemCard } from '@/components/ProblemCard'
import { formatRelative } from '@/utils/time'

import {
  engineLabel,
  engineOf,
  gateMatrixView,
  inferGatesMode,
  limitsView,
  parseConfigurableJson,
  phaseLabel,
  promptPinsView,
  scopeView,
  selectedPhasesView,
} from './configView'
import './golden-configs.css'

const EM_DASH = '—'

function GateCell({ mode }: { mode: 'gated' | 'auto' }) {
  return <span className={`gc-gate-cell gc-gate-cell--${mode}`}>{mode}</span>
}

/** Compact read-only 7x2 gate matrix (own rendering — nothing shared with the wizard). */
function GateMatrix({ configurable }: { configurable: Record<string, unknown> }) {
  const matrix = gateMatrixView(configurable['gates'])
  const planned = new Set(selectedPhasesView(configurable))
  return (
    <div className="data-table-wrap">
      <table className="data-table gc-gate-table" data-testid="gc-gate-matrix">
        <thead>
          <tr>
            <th>Phase</th>
            <th>Prompt review</th>
            <th>Output review</th>
          </tr>
        </thead>
        <tbody>
          {PHASE_NAMES.map((phase) => (
            <tr key={phase} className={planned.has(phase) ? undefined : 'gc-phase-skipped'}>
              <td>
                {phaseLabel(phase)}
                {!planned.has(phase) && <span className="gc-skip-note"> (not in plan)</span>}
              </td>
              <td>
                <GateCell mode={matrix[phase].prompt_review} />
              </td>
              <td>
                <GateCell mode={matrix[phase].output_review} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function ReadSections({ entry }: { entry: GoldenConfigEntry }) {
  const configurable = entry.configurable
  const scope = scopeView(configurable)
  const applications = useApplications(scope.project ?? undefined)
  const environments = useEnvironments(scope.app)
  const application = applications.data?.find((item) => item.id === scope.app)
  const environment = environments.data?.find((item) => item.id === scope.environment)
  const engine = engineOf(configurable)
  const pins = promptPinsView(configurable)
  const limits = limitsView(configurable)
  const phases = selectedPhasesView(configurable)

  return (
    <div className="gc-sections">
      <section className="glass-panel gc-section" aria-label="Scope defaults">
        <h3 className="gc-section-title">Scope defaults</h3>
        <dl className="gc-kv">
          <dt>Project</dt>
          <dd>{scope.project ? <ProjectIdentity projectId={scope.project} /> : EM_DASH}</dd>
          <dt>Application</dt>
          <dd>{application?.name ?? (scope.app ? 'Name unavailable' : EM_DASH)}{scope.app && <small> · <code>{scope.app}</code></small>}</dd>
          <dt>Environment</dt>
          <dd>{environment?.name ?? (scope.environment ? 'Name unavailable' : EM_DASH)}{scope.environment && <small> · <code>{scope.environment}</code></small>}</dd>
        </dl>
      </section>

      <section className="glass-panel gc-section" aria-label="Engine">
        <h3 className="gc-section-title">Engine</h3>
        <div className="gc-card-chips">
          <span className="dash-context-chip gc-chip-engine">{engineLabel(engine)}</span>
          <span className="dash-context-chip">
            {phases.length} of {PHASE_NAMES.length} phases in plan
          </span>
        </div>
      </section>

      <section className="glass-panel gc-section gc-section--wide" aria-label="Gate policy">
        <h3 className="gc-section-title">
          Gate policy <span className="dash-context-chip">{inferGatesMode(configurable['gates'])}</span>
        </h3>
        <GateMatrix configurable={configurable} />
      </section>

      <section className="glass-panel gc-section" aria-label="Prompt overrides">
        <h3 className="gc-section-title">Prompt overrides</h3>
        {pins.length === 0 ? (
          <p className="gc-muted">No prompt pins — phases use the latest catalog versions.</p>
        ) : (
          <ul className="gc-pin-list">
            {pins.map((pin) => (
              <li key={pin.key} className="gc-pin">
                <code className="gc-pin-key">{pin.key}</code>
                {pin.kind === 'version' ? (
                  <span className="dash-context-chip">version {pin.detail}</span>
                ) : pin.kind === 'content' ? (
                  <span className="dash-context-chip">inline content</span>
                ) : (
                  <span className="dash-context-chip">empty pin</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="glass-panel gc-section" aria-label="Limits">
        <h3 className="gc-section-title">Limits</h3>
        <dl className="gc-kv">
          {limits.map((limit) => (
            <div key={limit.key} className="gc-kv-row">
              <dt>{limit.label}</dt>
              <dd>
                {limit.value}
                {!limit.pinned && <span className="gc-muted"> (default)</span>}
              </dd>
            </div>
          ))}
        </dl>
      </section>

      <details className="glass-panel gc-section gc-section--wide gc-raw" data-testid="gc-raw-json">
        <summary>Raw configurable JSON</summary>
        <pre className="gc-raw-pre">{JSON.stringify(configurable, null, 2)}</pre>
      </details>
    </div>
  )
}

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
  const navigate = useNavigate()
  const consumer = useConsumer()
  const canEdit = isGlobalAdmin(consumer)
  const canStartRun = canLaunchPipelines(consumer)
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

  return (
    <section className="gc-page animate-enter">
      <Link to="/test-presets">All test presets</Link>
      {query.isError && (
        <CachedDataWarning error={query.error} onRetry={() => void query.refetch()} />
      )}
      <header className="gc-detail-header glass-panel">
        <div className="gc-detail-title-block">
          <h2 className="gc-page-title">{entry.name}</h2>
          <div className="gc-card-chips">
            {entry.isSystemDefault && (
              <span className="topbar-meta-chip info" data-testid="gc-system-chip">
                system default
              </span>
            )}
            <span className="dash-context-chip">v{entry.version}</span>
            <span className="dash-context-chip" title={entry.updatedAt}>
              updated {formatRelative(entry.updatedAt)}
            </span>
          </div>
          {entry.description && <p className="gc-card-description">{entry.description}</p>}
        </div>
        <div className="gc-actions-row">
          {writePending && <span className="topbar-meta-chip warning">saving…</span>}
          {!canEdit && <span className="gc-muted">Editing presets requires an unscoped admin key.</span>}
          {!editing && (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={writePending || !canEdit}
              onClick={() => {
                if (!writePending && canEdit) setEditing(true)
              }}
            >
              Edit preset
            </button>
          )}
          {canStartRun && (
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={writePending}
              onClick={() =>
                void navigate(`/runs/new?step=config&golden=${encodeURIComponent(entry.assistantId)}`)
              }
            >
              New test from this preset
            </button>
          )}
        </div>
      </header>

      {editing ? (
        <EditPanel
          entry={entry}
          onClose={() => setEditing(false)}
        />
      ) : (
        <ReadSections entry={entry} />
      )}
    </section>
  )
}

export function GoldenConfigDetailPage() {
  useTimestampPreference()
  const { assistantId = '' } = useParams()
  // Reset edit state and resource-bound hooks whenever the route identity changes.
  return <GoldenConfigDetail key={assistantId} assistantId={assistantId} />
}
