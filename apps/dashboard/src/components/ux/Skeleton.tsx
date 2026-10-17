import '@/theme/states.css'

/** One loading treatment and animation, with one announced label per region. */
export function Skeleton({ label, rows = 3, height = 44, className = '', 'data-testid': testId }: {
  label: string
  rows?: number
  height?: number | string
  className?: string
  'data-testid'?: string
}) {
  return <div className={`ux-skeleton ${className}`} data-testid={testId} role="status" aria-busy="true" aria-label={label}>
    <span className="ux-skeleton-label">{label.replace(/[….]+$/, '')}…</span>
    {Array.from({ length: Math.max(1, Math.min(20, Math.floor(rows))) }, (_, index) => (
      <div key={index} className="ux-skeleton-row" style={{ height }} aria-hidden="true" />
    ))}
  </div>
}
