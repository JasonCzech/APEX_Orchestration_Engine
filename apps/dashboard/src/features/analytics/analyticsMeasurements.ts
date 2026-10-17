import type { AgentAnalyticsSeriesPoint } from '@/api/hooks/useAgentAnalytics'
import type { GroupBy, Measure } from './analyticsFilters'

type Measurement = Pick<AgentAnalyticsSeriesPoint, 'total_tokens' | 'cost_usd' | 'avg_latency_ms'> & { latency_samples?: number | null }

export function metricValue(row: Measurement, measure: Measure): number | null {
  const value = measure === 'cost' ? row.cost_usd : measure === 'latency' ? row.avg_latency_ms : row.total_tokens
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

export function latencySamples(row: Measurement): number | null {
  return typeof row.latency_samples === 'number' && Number.isSafeInteger(row.latency_samples) && row.latency_samples >= 0 ? row.latency_samples : null
}

/** SQL averages ignore null timing values, so event count is not a valid weight. */
export function aggregateMeasurements(rows: readonly Measurement[], measure: Measure): number | null {
  let sum = 0
  let samples = 0
  for (const row of rows) {
    const value = metricValue(row, measure)
    if (value === null) continue
    const weight = measure === 'latency' ? latencySamples(row) : 1
    if (weight === null) return null
    if (weight === 0) continue
    sum += value * weight
    samples += weight
    if (!Number.isFinite(sum) || !Number.isSafeInteger(samples)) return null
  }
  if (samples === 0) return null
  const result = measure === 'latency' ? sum / samples : sum
  return Number.isFinite(result) ? result : null
}

function groupRows(rows: readonly AgentAnalyticsSeriesPoint[], keyOf: (row: AgentAnalyticsSeriesPoint) => string) {
  const grouped = new Map<string, AgentAnalyticsSeriesPoint[]>()
  for (const row of rows) {
    const key = keyOf(row)
    const group = grouped.get(key) ?? []
    group.push(row)
    grouped.set(key, group)
  }
  return grouped
}

export function rankedMeasurements(rows: readonly AgentAnalyticsSeriesPoint[], measure: Measure) {
  return Array.from(groupRows(rows, row => row.key), ([key, grouped]) => ({ key, value: aggregateMeasurements(grouped, measure) }))
    .filter((row): row is { key: string; value: number } => row.value !== null)
    .sort((a, b) => b.value - a.value || a.key.localeCompare(b.key))
}

export function measurementSeries(rows: readonly AgentAnalyticsSeriesPoint[], measure: Measure, group: GroupBy) {
  const buckets = groupRows(rows, row => row.bucket_start)
  if (group === 'date') {
    return { keys: [measure === 'latency' ? 'Average' : 'Total'], fields: ['total'], series: Array.from(buckets, ([bucket_start, grouped]) => ({ bucket_start, total: aggregateMeasurements(grouped, measure) })).sort((a, b) => a.bucket_start.localeCompare(b.bucket_start)) }
  }
  // Chart keys come from the chart population, never the paginated breakdown.
  const ranked = rankedMeasurements(rows, measure).map(row => row.key)
  const keys = ranked.slice(0, 5)
  const rest = ranked.slice(5)
  const fields = keys.map((_, index) => `series_${index}`)
  const series = Array.from(buckets, ([bucket_start, grouped]) => {
    const bucket: Record<string, string | number | null> = { bucket_start }
    keys.forEach((key, index) => { bucket[fields[index]!] = aggregateMeasurements(grouped.filter(row => row.key === key), measure) })
    if (rest.length) bucket.other = aggregateMeasurements(grouped.filter(row => rest.includes(row.key)), measure)
    return bucket
  }).sort((a, b) => String(a.bucket_start).localeCompare(String(b.bucket_start)))
  return { keys: rest.length ? [...keys, 'Other'] : keys, fields: rest.length ? [...fields, 'other'] : fields, series }
}
