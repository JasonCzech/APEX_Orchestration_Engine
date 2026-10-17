import { useTimestampPreference } from '@/theme/preferences'
import { StateCard } from '@/components/ux/StateCard'
import { Skeleton } from '@/components/ux/Skeleton'
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
      to={`/test-presets/${entry.assistantId}`}
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
  useTimestampPreference()
  const configs = useGoldenConfigsIndex()

  return (
    <section className="gc-page animate-enter">
      <header className="gc-toolbar glass-panel">
        <p className="gc-page-hint">
          Reusable test settings that pin an engine, reviews, phases and prompt versions. Choose
          one in the test wizard to inherit its bundle.
        </p>
      </header>

      {configs.isError && configs.data && (
        <CachedDataWarning error={configs.error} onRetry={() => void configs.refetch()} />
      )}

      {configs.isPending ? (
        <Skeleton label="Loading test presets" rows={SKELETON_CARDS} />
      ) : configs.isError && !configs.data ? (
        <ProblemCard
          title="Test presets unavailable"
          message="Test presets could not be loaded. Try again, or check the system status if the problem continues."
          onRetry={() => void configs.refetch()}
        />
      ) : (configs.data ?? []).length === 0 ? (
        <StateCard kind="empty" title="No test presets yet" action={<Link className="btn btn-secondary" to="/runs">Open tests</Link>}>
          <p>Ask an administrator to publish a preset for reusable test settings. Tests can also be authored without a preset.</p>
        </StateCard>
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
