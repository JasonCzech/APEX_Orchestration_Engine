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
 * [Start run with this config] deep-links the wizard's Config step with
 * ?golden=<assistant_id>; the wizard preselects the matching golden config.
 */
import { useState } from 'react'
import { useNavigate, useParams } from 'react-router'

import { PHASE_NAMES } from '@apex/pipeline-events'

import {
  useGoldenConfig,
  useUpdateGoldenConfig,
  type GoldenConfigEntry,
} from '@/api/hooks/useAssistants'
import { useConsumer } from '@/auth/AuthProvider'
import { roleAtLeast } from '@/auth/RequireRole'
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
          <dd>{scope.project ?? EM_DASH}</dd>
          <dt>Application</dt>
          <dd>{scope.app ?? EM_DASH}</dd>
          <dt>Environment</dt>
          <dd>{scope.environment ?? EM_DASH}</dd>
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

function EditPanel({ entry, onClose }: { entry: GoldenConfigEntry; onClose: () => void }) {
  const update = useUpdateGoldenConfig()
  const [text, setText] = useState(() => JSON.stringify(entry.configurable, null, 2))
  const parse = parseConfigurableJson(text)
  const canSave = parse.ok && !update.isPending

  function save() {
    if (!parse.ok) return
    update.mutate(
      { assistantId: entry.assistantId, configurable: parse.value },
      { onSuccess: onClose },
    )
  }

  return (
    <section className="glass-panel gc-section gc-section--wide" aria-label="Edit configurable">
      <h3 className="gc-section-title">Edit configurable</h3>
      <p className="gc-muted">
        The full config.configurable bundle, replaced on save. Saving publishes a new assistant
        version.
      </p>
      <textarea
        className="field-input gc-json-editor"
        aria-label="Configurable JSON"
        rows={16}
        spellCheck={false}
        value={text}
        onChange={(event) => setText(event.target.value)}
      />
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
          onClick={onClose}
          disabled={update.isPending}
        >
          Cancel
        </button>
        <button type="button" className="btn btn-primary btn-sm" disabled={!canSave} onClick={save}>
          {update.isPending ? 'Saving…' : 'Save new version'}
        </button>
      </div>
    </section>
  )
}

export function GoldenConfigDetailPage() {
  const { assistantId = '' } = useParams()
  const navigate = useNavigate()
  const consumer = useConsumer()
  const canEdit = consumer ? roleAtLeast(consumer.role, 'admin') : false
  const canStartRun = consumer ? roleAtLeast(consumer.role, 'operator') : false
  const query = useGoldenConfig(assistantId)
  const [editing, setEditing] = useState(false)

  if (query.isPending) {
    return (
      <div className="gc-skeleton" role="status" aria-busy="true" aria-label="Loading golden config">
        <div className="glass-panel gc-skeleton-card" />
        <div className="glass-panel gc-skeleton-card" />
      </div>
    )
  }

  if (query.isError) {
    return (
      <ProblemCard
        title="Golden config unavailable"
        message={query.error.message}
        onRetry={() => void query.refetch()}
      />
    )
  }

  const entry = query.data

  return (
    <section className="gc-page animate-enter">
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
          {canEdit && !editing && (
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setEditing(true)}>
              Edit JSON
            </button>
          )}
          {canStartRun && (
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() =>
                void navigate(`/runs/new?step=config&golden=${encodeURIComponent(entry.assistantId)}`)
              }
            >
              Start run with this config
            </button>
          )}
        </div>
      </header>

      {editing ? (
        <EditPanel entry={entry} onClose={() => setEditing(false)} />
      ) : (
        <ReadSections entry={entry} />
      )}
    </section>
  )
}
