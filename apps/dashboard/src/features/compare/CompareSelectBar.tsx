import { Link } from 'react-router'

import './compare.css'

/**
 * Floating action bar for the runs-grid compare selection (D8): appears once
 * 2+ runs are ticked, linking to /runs/compare?ids=… . Owned by the compare
 * feature so the RunsListPage edit stays a small, additive affordance.
 */
export function CompareSelectBar({
  selected,
  onClear,
}: {
  selected: readonly string[]
  onClear: () => void
}) {
  if (selected.length < 2) return null
  return (
    <div className="compare-select-bar glass-panel" role="region" aria-label="Compare selection">
      <span className="compare-select-count">{selected.length} selected</span>
      <Link className="btn btn-primary btn-sm compare-select-cta" to={`/runs/compare?ids=${selected.join(',')}`}>
        Compare ({selected.length})
      </Link>
      <button type="button" className="btn btn-ghost btn-sm" onClick={onClear}>
        Clear
      </button>
    </div>
  )
}
