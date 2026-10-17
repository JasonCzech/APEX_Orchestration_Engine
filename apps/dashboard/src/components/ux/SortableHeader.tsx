import type { ReactNode } from 'react'

export function SortableHeader({ children, active, direction, onSort, label, className = '' }: {
  children: ReactNode; active: boolean; direction: 'asc' | 'desc'; onSort: () => void
  label?: string; className?: string
}) {
  return <th scope="col" aria-sort={active ? direction === 'asc' ? 'ascending' : 'descending' : 'none'}>
    <button type="button" className={`btn btn-ghost btn-sm ${className}`.trim()} aria-label={label} onClick={onSort}>
      {children}<span aria-hidden="true">{active ? direction === 'asc' ? ' ↑' : ' ↓' : ''}</span>
    </button>
  </th>
}
