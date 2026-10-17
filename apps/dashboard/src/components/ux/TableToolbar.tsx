import type { ReactNode } from 'react'

/** Shared wrapping controls with the same active-filter and clearing convention. */
export function TableToolbar({ label, children, filterCount = 0, onClear, className = '' }: {
  label: string; children: ReactNode; filterCount?: number; onClear?: () => void; className?: string
}) {
  return <div role="group" aria-label={label} className={`ux-actions ux-table-toolbar ${className}`.trim()}>
    {children}
    {filterCount > 0 && <span className="status-badge neutral">{filterCount} active {filterCount === 1 ? 'filter' : 'filters'}</span>}
    {filterCount > 0 && onClear && <button type="button" className="btn btn-ghost btn-sm" onClick={onClear}>Clear filters</button>}
  </div>
}
