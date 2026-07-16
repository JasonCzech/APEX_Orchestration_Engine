/**
 * /golden-configs — golden configurations = LangGraph assistants on the
 * `pipeline` graph pinning a config.configurable bundle (plan Part 2 route
 * table + "Golden configurations"). Viewer-visible read surface: unlike the
 * wizard picker, the index INCLUDES the dev server's auto-created default
 * assistant, marked with a "system default" chip.
 */
import { Link } from 'react-router'

import { useGoldenConfigsIndex, type GoldenConfigEntry } from '@/api/hooks/useAssistants'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { ProblemCard } from '@/components/ProblemCard'
import { formatRelative } from '@/utils/time'

import { engineLabel, summarizeConfigurable } from './configView'
import './golden-configs.css'

const SKELETON_CARDS = 3

function GoldenConfigCard({ entry }: { entry: GoldenConfigEntry }) {
  const summary = summarizeConfigurable(entry.configurable)
  return (
    <Link
      to={`/golden-configs/${entry.assistantId}`}
      className="glass-panel gc-card"
      data-testid={`gc-card-${entry.assistantId}`}
    >
      <div className="gc-card-head">
        <span className="gc-card-name">{entry.name}</span>
        {entry.isSystemDefault && (
          <span className="topbar-meta-chip info" data-testid="gc-system-chip">
            system default
          </span>
        )}
      </div>
      {entry.description && <p className="gc-card-description">{entry.description}</p>}
      <div className="gc-card-chips">
        <span className="dash-context-chip gc-chip-engine">{engineLabel(summary.engine)}</span>
        <span className="dash-context-chip">{summary.gatesMode}</span>
        <span className="dash-context-chip">
          {summary.phaseCount} {summary.phaseCount === 1 ? 'phase' : 'phases'}
        </span>
        <span className="dash-context-chip">
          {summary.promptPins} {summary.promptPins === 1 ? 'prompt pin' : 'prompt pins'}
        </span>
      </div>
      <div className="gc-card-foot">
        <span>v{entry.version}</span>
        <span title={entry.updatedAt}>updated {formatRelative(entry.updatedAt)}</span>
      </div>
    </Link>
  )
}

export function GoldenConfigsPage() {
  const configs = useGoldenConfigsIndex()

  return (
    <section className="gc-page animate-enter">
      <header className="gc-toolbar glass-panel">
        {/* The topbar renders the route-handle h1; this is a section label. */}
        <h2 className="gc-page-title">Golden configurations</h2>
        <p className="gc-page-hint">
          Published assistants pinning engine, gates, phases and prompt versions. Pick one in the
          run wizard to inherit its bundle.
        </p>
      </header>

      {configs.isError && configs.data && (
        <CachedDataWarning error={configs.error} onRetry={() => void configs.refetch()} />
      )}

      {configs.isPending ? (
        <div
          className="gc-skeleton"
          role="status"
          aria-busy="true"
          aria-label="Loading golden configs"
        >
          {Array.from({ length: SKELETON_CARDS }, (_, i) => (
            <div key={i} className="glass-panel gc-skeleton-card" />
          ))}
        </div>
      ) : configs.isError && !configs.data ? (
        <ProblemCard
          title="Golden configs unavailable"
          message={configs.error.message}
          onRetry={() => void configs.refetch()}
        />
      ) : (configs.data ?? []).length === 0 ? (
        <div className="dash-empty">
          <h2>No golden configs yet</h2>
          <p className="dash-empty-hint">
            Golden configurations are LangGraph assistants on the pipeline graph — publish one to
            pin a reusable run bundle.
          </p>
        </div>
      ) : (
        <div className="gc-grid">
          {(configs.data ?? []).map((entry) => (
            <GoldenConfigCard key={entry.assistantId} entry={entry} />
          ))}
        </div>
      )}
    </section>
  )
}
