import { useId, type ReactNode } from 'react'
import '@/theme/states.css'

export type StateKind = 'empty' | 'filtered' | 'loading' | 'not-found' | 'unauthorized' | 'unconfigured' | 'stale'
const SYMBOLS: Record<StateKind, string> = {
  empty: '○', filtered: '⌕', loading: '◌', 'not-found': '?', unauthorized: '⊘', unconfigured: '⚙', stale: '↻',
}

/** Explicit state and next action keep absence, access and loading distinct. */
export function StateCard({ kind, title, children, action, compact = false, className = '', 'data-testid': testId }: {
  kind: StateKind
  title: string
  children?: ReactNode
  /** Use null only when the state has no available action. */
  action: ReactNode
  compact?: boolean
  className?: string
  'data-testid'?: string
}) {
  const titleId = useId()
  return <section className={`ux-state-card ${compact ? 'compact' : ''} ${className}`} data-state={kind} data-testid={testId}
    role={kind === 'loading' ? 'status' : 'region'} aria-labelledby={titleId} aria-busy={kind === 'loading' || undefined}>
    <span className="ux-state-icon" aria-hidden="true">{SYMBOLS[kind]}</span>
    <div className="ux-state-content">
      <h2 id={titleId}>{title}</h2>
      {children && <div className="ux-state-description">{children}</div>}
      {action && <div className="ux-state-actions">{action}</div>}
    </div>
  </section>
}
