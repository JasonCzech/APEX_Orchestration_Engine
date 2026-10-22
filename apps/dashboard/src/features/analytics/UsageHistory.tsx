import { useTimestampPreference } from '@/theme/preferences'
import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getApexClient } from '@/api/apexClient'
import { useOptionalConsumer } from '@/auth/AuthProvider'
import { formatTimestamp } from '@/utils/time'
import './modelPricing.css'

type Event = { id: string; at: string; model: string | null; archived: boolean; project_id: string | null; app_id: string | null; consumer_id: string | null; consumer_name_snapshot: string | null; total_tokens: number; status: string; original_cost_usd: string | null; effective_cost_usd: string | null }
type UsageDetail = Event & { cost_visible: boolean; original_pricing?: Record<string, string> | null; phase: string; input_tokens: number; output_tokens: number; cache_read_tokens: number; cache_creation_tokens: number; latency_ms: number | null; corrections?: { cost_usd: string; at: string; generation: number; pricing?: Record<string, string> | null }[] }
type HistoryParams = { from?: string; to?: string; view?: 'mine' | 'authorized'; consumer_id?: string; project?: string; app?: string; model_id?: string; provider?: string; model?: string; stage?: string; agent?: string; test?: string; status?: 'ok' | 'error'; cost_basis?: 'original' | 'corrected' }
export function UsageHistory({ modelId, filters = {} }: { modelId?: string; filters?: HistoryParams }) {
  useTimestampPreference()
  const consumer = useOptionalConsumer()
  const view = filters.view ?? (consumer?.role === 'admin' ? 'authorized' : 'mine')
  const [window] = useState(() => { const to = new Date(); return { from: new Date(to.getTime() - 7 * 86400000).toISOString(), to: to.toISOString() } })
  const [cursor, setCursor] = useState<string | undefined>()
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [exportError, setExportError] = useState('')
  const [exporting, setExporting] = useState(false)
  const exportRequest = useRef<AbortController | null>(null)
  useEffect(() => () => exportRequest.current?.abort(), [])
  const [selected, setSelected] = useState<string | null>(null)
  const to = end ? new Date(end).toISOString() : filters.to ?? window.to
  const from = start ? new Date(start).toISOString() : filters.from ?? new Date(Date.parse(to) - 7 * 86400000).toISOString()
  const query: HistoryParams = { ...filters, from, to, view, ...(modelId ? { model_id: modelId } : {}) }
  const history = useQuery({ queryKey: ['analytics', 'history', query, cursor], queryFn: async () => {
    const r = await getApexClient().GET('/v1/analytics/agents/events', { params: { query: { ...query, cursor, limit: 20 } } })
    if (!r.data) throw new Error('Usage history could not be loaded. Check your date interval and permissions.')
    return r.data as unknown as { items: Event[]; next_cursor: string | null; cost_visible: boolean }
  } })
  const detail = useQuery({ queryKey: ['analytics', 'event', view, selected], enabled: !!selected, queryFn: async () => { const r = await getApexClient().GET('/v1/analytics/agents/events/{event_id}', { params: { path: { event_id: selected! }, query: { view } } }); if (!r.data) throw new Error('Event unavailable'); return r.data as unknown as UsageDetail } })
  async function download() {
    const controller = new AbortController()
    exportRequest.current = controller
    setExporting(true); setExportError('')
    try {
      const r = await getApexClient().GET('/v1/analytics/agents/export', { params: { query }, parseAs: 'blob', signal: controller.signal })
      if (controller.signal.aborted) return
      if (!r.response.ok || !r.data) throw new Error('Export failed. Try a narrower interval (maximum 100,000 events).')
      const url = URL.createObjectURL(r.data as Blob)
      const a = document.createElement('a'); a.href = url; a.download = 'agent-usage.csv'; a.click(); URL.revokeObjectURL(url)
    } catch (e) { setExportError(e instanceof Error ? e.message : 'Export failed') } finally { setExporting(false) }
  }
  return <section className="glass-panel pricing-history"><header className="pricing-heading"><div><h2>Usage history</h2><p>Retained invocation records, including archived models.</p></div><button className="btn btn-secondary" disabled={exporting} onClick={() => void download()}>{exporting ? 'Exporting…' : 'Export CSV'}</button></header>
    {modelId && <div className="pricing-toolbar"><label>From (local time)<input type="datetime-local" value={start} onChange={e => { setStart(e.target.value); setCursor(undefined) }} /></label><label>To, exclusive (local time)<input type="datetime-local" value={end} onChange={e => { setEnd(e.target.value); setCursor(undefined) }} /></label></div>}
    {exportError && <p role="alert">{exportError}</p>}{history.error && <p role="alert">{history.data ? 'Showing cached data. ' : ''}{history.error.message} <button onClick={() => void history.refetch()}>Retry</button></p>}{history.isPending && <p role="status">Loading usage…</p>}
    {history.data && <><div className="pricing-table-wrap"><table className="pricing-table"><thead><tr><th>Time</th><th>Model</th><th>Application</th><th>User</th><th className="numeric">Tokens</th><th className="numeric">Estimated cost</th><th>Status</th></tr></thead><tbody>{history.data.items.map(event => <tr key={event.id}><td><button className="analytics-link-button" onClick={() => setSelected(event.id)}>{formatTimestamp(event.at)}</button></td><td>{event.model ?? 'Unknown'}{event.archived && <small>Archived</small>}</td><td>{event.app_id ?? 'Unknown'}<small>{event.project_id}</small></td><td>{event.consumer_name_snapshot ?? event.consumer_id ?? 'Unattributed'}</td><td className="numeric">{event.total_tokens.toLocaleString()}</td><td className="numeric">{!history.data.cost_visible ? 'Hidden' : event.effective_cost_usd === null ? 'Not priced' : `$${event.effective_cost_usd}`}</td><td>{event.status}</td></tr>)}{!history.data.items.length && <tr><td colSpan={7}>No usage in this interval.</td></tr>}</tbody></table></div><footer className="pricing-pagination"><button disabled={!cursor} onClick={() => setCursor(undefined)}>First page</button><button disabled={!history.data.next_cursor} onClick={() => setCursor(history.data!.next_cursor!)}>Next page</button></footer></>}
    {selected && <section className="pricing-preview" aria-label="Usage event details"><button onClick={() => setSelected(null)}>Close details</button><h3>Usage and pricing provenance</h3>{detail.isPending ? <p>Loading details…</p> : detail.error ? <p role="alert">{detail.error.message}</p> : <><dl>{[
      ['Model', detail.data?.model], ['Application', detail.data?.app_id], ['User', detail.data?.consumer_name_snapshot ?? detail.data?.consumer_id], ['Phase', detail.data?.phase],
      ['Input tokens', detail.data?.input_tokens?.toLocaleString()], ['Output tokens', detail.data?.output_tokens?.toLocaleString()], ['Cache-read tokens', detail.data?.cache_read_tokens?.toLocaleString()], ['Cache-write tokens', detail.data?.cache_creation_tokens?.toLocaleString()],
      ['Original estimated cost', detail.data?.cost_visible === false || history.data?.cost_visible === false ? 'Hidden' : detail.data?.original_cost_usd != null ? `$${detail.data.original_cost_usd}` : 'Not priced'], ['Corrected estimated cost', detail.data?.cost_visible === false || history.data?.cost_visible === false ? 'Hidden' : detail.data?.effective_cost_usd != null ? `$${detail.data.effective_cost_usd}` : 'Not priced']
    ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value ?? 'Not available'}</dd></div>)}</dl>{detail.data?.original_pricing && <section><h4>Original rate snapshot</h4><p>USD per million tokens</p><dl>{([['input', 'Input'], ['output', 'Output'], ['cache_read', 'Cache read'], ['cache_creation', 'Cache write']] as const).map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{detail.data?.original_pricing?.[key] != null ? `$${detail.data.original_pricing[key]}` : 'Not recorded'}</dd></div>)}</dl></section>}{!!detail.data?.corrections?.length && <><h4>Published cost corrections</h4><ul>{detail.data.corrections.map(c => <li key={c.generation}>Revision {c.generation} · ${c.cost_usd} · {formatTimestamp(c.at)}{c.pricing && <p>USD / million tokens: Input ${c.pricing.input}, output ${c.pricing.output}, cache read ${c.pricing.cache_read}, cache write ${c.pricing.cache_creation}</p>}</li>)}</ul></>}</>}</section>}
  </section>
}
