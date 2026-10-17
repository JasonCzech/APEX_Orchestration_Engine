/** Error-rate contracts carry fractions, not percentages. */
export function formatPercentRate(rate: number | null | undefined): string {
  return typeof rate === 'number' && Number.isFinite(rate) ? `${(rate * 100).toFixed(2)}%` : '—'
}
