/** Home shortcut (F-09): start a test from your most-used presets. */
import { Link } from 'react-router'

import { usePresetUsage, usePresets } from '@/api/hooks/useTestPresets'
import { useFieldNames } from '@/features/presets/useFieldNames'

export function HomePresets() {
  const presets = usePresets()
  const active = (presets.data ?? []).filter(preset => !preset.archived)
  const usage = usePresetUsage(active)
  const names = useFieldNames()
  if (active.length === 0) return null
  const top = [...active]
    .sort((a, b) => (usage.data?.get(b.key)?.tests ?? 0) - (usage.data?.get(a.key)?.tests ?? 0) || Number(b.mine) - Number(a.mine) || a.name.localeCompare(b.name))
    .slice(0, 3)
  return (
    <section className="glass-panel home-drafts" data-testid="home-presets">
      <div className="home-section-head">
        <h2 className="home-section-title">Start from a preset</h2>
        <Link to="/test-presets">All presets</Link>
      </div>
      <div className="home-draft-row-list">
        {top.map(preset => (
          <Link key={preset.key} to={`/runs/new?preset=${encodeURIComponent(preset.key)}`} className="home-draft-row">
            <span className="home-draft-title"><span>{preset.name}</span> · {names.applicationName(preset.applicationId)}</span>
            <span className="home-draft-updated">{usage.data?.get(preset.key)?.tests ?? 0} tests</span>
          </Link>
        ))}
      </div>
    </section>
  )
}
