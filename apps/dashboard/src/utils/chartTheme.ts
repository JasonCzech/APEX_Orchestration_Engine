import type { CSSProperties } from 'react'

/** Recharts tooltips use the same readable surface across dashboard charts. */
export const CHART_TOOLTIP_STYLE: CSSProperties = {
  background: 'var(--bg-elevated)',
  border: '1px solid var(--border)',
  borderRadius: 'var(--radius-sm)',
  color: 'var(--text-primary)',
  fontSize: 'var(--font-size-xs)',
}
