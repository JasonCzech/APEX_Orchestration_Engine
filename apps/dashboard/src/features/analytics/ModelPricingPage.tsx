import { useTimestampPreference } from '@/theme/preferences'
import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getApexClient } from '@/api/apexClient'
import { errorMessageOf } from '@/api/errors'
import { useOptionalConsumer } from '@/auth/AuthProvider'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { Dialog } from '@/components/Dialog'
import { ProblemCard } from '@/components/ProblemCard'
import { OverflowMenu } from '@/components/ux/OverflowMenu'
import { Pagination } from '@/components/ux/Pagination'
import { Skeleton } from '@/components/ux/Skeleton'
import { StateCard } from '@/components/ux/StateCard'
import { TabList } from '@/components/ux/TabList'
import { TableToolbar } from '@/components/ux/TableToolbar'
import { formatTimestamp } from '@/utils/time'
import { UsageHistory } from './UsageHistory'
import './modelPricing.css'

type Rates = { input: string; output: string; cache_read: string; cache_creation: string }
type Price = Rates & { id: string; effective_from: string; effective_to: string | null }
type Model = { id: string; model_key: string; provider: string; availability: string; version: number; current_price: Price | null; active_correction_id?: string | null }
type Revision = { id: string; number: number; reason: string; creator_name: string | null; created_at: string; published_at: string | null; intervals: Price[] }
type Job = { id: string; state: string; processed_count: number; failure_code: string | null }
type Preview = { preview_token: string; events: number; before_cost_usd: string; after_cost_usd: string; delta_cost_usd: string; previously_unpriced: number; calculated_at: string }
const fields: [keyof Rates, string][] = [['input', 'Input'], ['output', 'Output'], ['cache_read', 'Cache read'], ['cache_creation', 'Cache write']]
const blank: Rates = { input: '', output: '', cache_read: '', cache_creation: '' }
const MODEL_PANEL_TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'history', label: 'Pricing history' },
  { id: 'usage', label: 'Usage history' },
] as const
type ModelPanelTab = (typeof MODEL_PANEL_TABS)[number]['id']

function fail(error: unknown): Error { return new Error(errorMessageOf(error, 'The request failed. Reload and try again.')) }
function editableRates(price: Rates | null): Rates {
  return price ? { input: price.input, output: price.output, cache_read: price.cache_read, cache_creation: price.cache_creation } : { ...blank }
}

export function ModelPricingPage() {
  useTimestampPreference()
  const consumer = useOptionalConsumer()
  const allowed = consumer?.role === 'admin' && consumer.scopes.length === 0
  const [params, setParams] = useSearchParams()
  const [selected, setSelected] = useState<Model | null>(null)
  const [archiveIntent, setArchiveIntent] = useState(false)
  const limit = Number(params.get('limit') ?? 20) || 20
  const offset = Number(params.get('offset') ?? 0) || 0
  const statusFilter = (params.get('status') ?? 'active') as 'active' | 'archived' | 'all'
  const queryText = params.get('q') ?? ''
  const providerFilter = params.get('provider') ?? ''
  const missingFilter = params.get('missing') === 'true'
  const activeFilterCount = [statusFilter !== 'active', Boolean(queryText), Boolean(providerFilter), missingFilter].filter(Boolean).length

  const query = useQuery({ queryKey: ['model-pricing', params.toString()], enabled: allowed, queryFn: async () => {
    const { data, error } = await getApexClient().GET('/v1/analytics/models', { params: { query: { status: statusFilter, q: queryText, provider: providerFilter || undefined, missing_pricing: missingFilter, offset, limit } } })
    if (!data) throw fail(error)
    return data as unknown as { items: Model[]; total: number }
  } })
  function filter(key: string, value: string) { setParams(prev => { const next = new URLSearchParams(prev); if (value) next.set(key, value); else next.delete(key); if (key !== 'offset') next.delete('offset'); return next }) }
  function clearFilters() { setParams(new URLSearchParams()) }
  if (!allowed) {
    return (
      <StateCard kind="unauthorized" title="Administrator access required" action={<Link className="btn btn-secondary btn-sm" to="/analytics">Return to your usage</Link>}>
        <p>Pricing management requires an unrestricted platform administrator.</p>
      </StateCard>
    )
  }
  return <section className="pricing-page animate-enter">
    <header className="pricing-heading"><div><Link to="/analytics">← Agent analytics</Link><h2>Rates &amp; availability</h2><p>Manage rates and model availability. Usage and price history are retained.</p></div><span className="pricing-unit">USD / million tokens</span></header>
    <TableToolbar label="Model pricing filters" className="glass-panel pricing-toolbar" filterCount={activeFilterCount} onClear={clearFilters}>
      <div role="group" aria-label="Model status">{['active', 'archived', 'all'].map(status => <button key={status} type="button" className="btn btn-secondary btn-sm" aria-pressed={statusFilter === status} onClick={() => filter('status', status)}>{status.charAt(0).toUpperCase() + status.slice(1)}</button>)}</div>
      <label>Find model<input type="search" className="field-input" value={queryText} onChange={e => filter('q', e.target.value)} placeholder="Search model name" /></label>
      <label>Provider<input className="field-input" value={providerFilter} onChange={e => filter('provider', e.target.value)} placeholder="All providers" /></label>
      <label className="pricing-checkbox"><input type="checkbox" checked={missingFilter} onChange={e => filter('missing', e.target.checked ? 'true' : '')} />Missing pricing</label>
    </TableToolbar>
    {query.isPending && <Skeleton label="Loading model pricing…" rows={4} />}
    {query.error && query.data && <CachedDataWarning error={query.error} onRetry={() => void query.refetch()} />}
    {query.error && !query.data && <ProblemCard title="Model pricing unavailable" message={query.error.message} onRetry={() => void query.refetch()} />}
    {query.data && (query.data.items.length === 0 ? (
      <StateCard
        kind={activeFilterCount > 0 ? 'filtered' : 'empty'}
        title={activeFilterCount > 0 ? 'No models match these filters' : 'No models registered'}
        action={activeFilterCount > 0 ? <button type="button" className="btn btn-secondary btn-sm" onClick={clearFilters}>Clear filters</button> : null}
      >
        <p>{activeFilterCount > 0 ? 'Clear or adjust the status, provider, or search filters to view models.' : 'Models appear here once registered or observed in agent invocations.'}</p>
      </StateCard>
    ) : (
      <div className="glass-panel pricing-table-wrap">
        <table className="pricing-table"><thead><tr><th>Model</th><th>Availability</th>{fields.map(([, label]) => <th key={label} className="numeric">{label}</th>)}<th>Effective from</th><th>Actions</th></tr></thead><tbody>
          {query.data.items.map(model => <tr key={model.id}><td><button type="button" className="analytics-link-button" onClick={() => { setArchiveIntent(false); setSelected(model) }}>{model.model_key}</button><small>{model.provider}</small></td><td><span className="pricing-badge">{model.availability.replace('_', ' ')}</span></td>{fields.map(([key]) => <td className="numeric" key={key}>{model.current_price ? `$${model.current_price[key].replace(/(\.\d*?[1-9])0+$|\.0+$/, '$1')}` : 'Not priced'}</td>)}<td>{model.current_price ? formatTimestamp(model.current_price.effective_from) : '—'}</td><td><button type="button" className="btn btn-secondary btn-sm" onClick={() => { setArchiveIntent(false); setSelected(model) }}>{model.current_price ? 'Edit pricing' : 'Set pricing'}</button><OverflowMenu label={`More actions for ${model.model_key}`} items={[{ label: model.availability === 'archived' ? 'Restore model' : 'Archive model', onSelect: () => { setArchiveIntent(true); setSelected(model) } }]} /></td></tr>)}
        </tbody></table>
        <footer className="pricing-pagination">
          <span>{query.data.total} {query.data.total === 1 ? 'model' : 'models'}</span>
          <Pagination
            label="Models"
            offset={offset}
            limit={limit}
            options={[10, 20, 50, 100]}
            hasNext={offset + limit < query.data.total}
            pending={query.isFetching}
            onChange={({ offset: nextOffset, limit: nextLimit }) => {
              setParams(prev => {
                const next = new URLSearchParams(prev)
                if (nextOffset > 0) next.set('offset', String(nextOffset))
                else next.delete('offset')
                if (nextLimit !== 20) next.set('limit', String(nextLimit))
                else next.delete('limit')
                return next
              })
            }}
          />
        </footer>
      </div>
    ))}
    {selected && <ModelPanel model={selected} archiveIntent={archiveIntent} onClose={() => setSelected(null)} />}
  </section>
}

function ModelPanel({ model, onClose, archiveIntent }: { model: Model; onClose: () => void; archiveIntent: boolean }) {
  const cache = useQueryClient()
  const [tab, setTab] = useState<ModelPanelTab>('overview')
  const [rates, setRates] = useState<Rates>(() => editableRates(model.current_price))
  const [reason, setReason] = useState('')
  const [mode, setMode] = useState<'now' | 'scheduled' | 'correction'>('now')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [preview, setPreview] = useState<Preview | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [key, setKey] = useState(() => crypto.randomUUID())
  const [confirmArchive, setConfirmArchive] = useState(archiveIntent)
  const [draftVersion, setDraftVersion] = useState(model.version)
  const [historyOffset, setHistoryOffset] = useState(0)
  const detail = useQuery({ queryKey: ['model-pricing', model.id], queryFn: async () => { const r = await getApexClient().GET('/v1/analytics/models/{model_id}', { params: { path: { model_id: model.id } } }); if (!r.data) throw fail(r.error); return r.data as unknown as Model } })
  const current = detail.data ?? model
  useEffect(() => { if (detail.data?.active_correction_id) setJobId(detail.data.active_correction_id) }, [detail.data?.active_correction_id])
  const revisions = useQuery({ queryKey: ['model-pricing', model.id, 'history', historyOffset], enabled: tab === 'history', queryFn: async () => { const r = await getApexClient().GET('/v1/analytics/models/{model_id}/prices', { params: { path: { model_id: model.id }, query: { limit: 20, offset: historyOffset } } }); if (!r.data) throw fail(r.error); return r.data as unknown as { items: Revision[]; total: number } } })
  const job = useQuery({ queryKey: ['model-pricing', model.id, 'job', jobId], enabled: !!jobId, refetchInterval: query => ['queued', 'running'].includes(query.state.data?.state ?? '') ? 1500 : false, queryFn: async () => { const r = await getApexClient().GET('/v1/analytics/models/{model_id}/corrections/{job_id}', { params: { path: { model_id: model.id, job_id: jobId! } } }); if (!r.data) throw fail(r.error); return r.data as unknown as Job } })
  const { refetch: refreshDetail } = detail
  const completedJob = useRef<string | null>(null)
  useEffect(() => {
    if (job.data?.state !== 'succeeded' || completedJob.current === job.data.id) return
    completedJob.current = job.data.id
    void cache.invalidateQueries({ queryKey: ['model-pricing'] })
    void cache.invalidateQueries({ queryKey: ['analytics'] })
    void refreshDetail().then(result => { if (result.data) setDraftVersion(result.data.version) })
  }, [job.data?.state, job.data?.id, cache, refreshDetail])
  function changed() { if (mutation.isPending) return; setPreview(null); mutation.reset(); setKey(crypto.randomUUID()) }
  const mutation = useMutation({ mutationFn: async (action: 'save' | 'preview' | 'correct' | 'archive' | 'retry') => {
    const path = { model_id: model.id }
    const common = { expected_version: draftVersion, reason, idempotency_key: key }
    const body = { ...common, rates, effective_from: start ? new Date(start).toISOString() : undefined }
    if (action === 'preview' || action === 'correct') {
      if (!start || !end) throw new Error('Choose both correction dates.')
      const correction = { ...body, effective_from: new Date(start).toISOString(), effective_to: new Date(end).toISOString(), preview_token: preview?.preview_token }
      const r = action === 'preview' ? await getApexClient().POST('/v1/analytics/models/{model_id}/corrections/preview', { params: { path }, body: correction }) : await getApexClient().POST('/v1/analytics/models/{model_id}/corrections', { params: { path }, body: correction })
      if (!r.data) throw fail(r.error)
      if (action === 'preview') setPreview(r.data as unknown as Preview)
      else { setJobId('id' in r.data ? r.data.id : null); setPreview(null) }
    } else if (action === 'save') {
      const r = await getApexClient().POST('/v1/analytics/models/{model_id}/prices', { params: { path }, body: { ...body, effective_from: mode === 'now' ? undefined : body.effective_from } }); if (!r.data) throw fail(r.error); setDraftVersion(Number(r.data.version))
    } else if (action === 'archive') {
      const r = current.availability === 'archived' ? await getApexClient().POST('/v1/analytics/models/{model_id}/restore', { params: { path }, body: common }) : await getApexClient().POST('/v1/analytics/models/{model_id}/archive', { params: { path }, body: common }); if (!r.data) throw fail(r.error); setDraftVersion(Number(r.data.version)); setConfirmArchive(false)
    } else {
      const r = await getApexClient().POST('/v1/analytics/models/{model_id}/corrections/{job_id}/retry', { params: { path: { ...path, job_id: jobId! } } }); if (!r.data) throw fail(r.error)
    }
    if (action !== 'preview') { setPreview(null); setKey(crypto.randomUUID()); await cache.invalidateQueries({ queryKey: ['model-pricing'] }); await cache.invalidateQueries({ queryKey: ['analytics'] }) }
  } })
  return <Dialog className="pricing-dialog" overlayClassName="ux-dialog-overlay" labelledBy="model-title" onClose={onClose}>
    <header className="pricing-heading"><div><small>{model.provider}</small><h2 id="model-title">{model.model_key}</h2><span className="pricing-badge">{current.availability.replace('_', ' ')}</span></div><button type="button" className="btn btn-secondary btn-sm" onClick={onClose} aria-label="Close model details">Close</button></header>
    <TabList id="model-pricing" label="Model details" items={[...MODEL_PANEL_TABS]} value={tab} onChange={setTab} />
    {detail.error && <ProblemCard title="Model details could not be refreshed" message={detail.error.message} onRetry={() => void detail.refetch()} />}
    {job.error && <ProblemCard title="Correction status could not be refreshed" message={job.error.message} onRetry={() => void job.refetch()} />}
    {mutation.error && <p role="alert">{mutation.error.message} <button type="button" className="btn btn-ghost btn-sm" onClick={() => { setPreview(null); void detail.refetch().then(result => { if (result.data) { setDraftVersion(result.data.version); setRates(editableRates(result.data.current_price)); setKey(crypto.randomUUID()) } }) }}>Reload model and reset draft</button></p>}
    {mutation.isSuccess && !preview && <p role="status">Change saved.</p>}
    {job.data && <p role="status">Correction {job.data.state} · {job.data.processed_count} events processed {job.data.state === 'failed' && <button type="button" className="btn btn-secondary btn-sm" onClick={() => mutation.mutate('retry')} disabled={mutation.isPending}>Retry correction</button>}</p>}
    <section hidden={tab !== 'overview'} id="model-pricing-panel-overview" role="tabpanel" aria-labelledby="model-pricing-tab-overview">
      {tab === 'overview' && <form onSubmit={e => { e.preventDefault(); mutation.mutate(mode === 'correction' ? 'preview' : 'save') }}>
        <h3>Token prices <small>USD per million tokens</small></h3><div className="pricing-rate-grid">{fields.map(([field, label]) => <label key={field}>{label}<input className="field-input" aria-label={label} disabled={mutation.isPending} required type="number" min="0" max="999999999.999999999" step="0.000000001" value={rates[field]} onChange={e => { setRates({ ...rates, [field]: e.target.value }); changed() }} />{field.startsWith('cache') && <button type="button" disabled={mutation.isPending} className="analytics-link-button" onClick={() => { setRates({ ...rates, [field]: rates.input }); changed() }}>Use input rate</button>}</label>)}</div>
        <label>Effective timing<select className="field-select" disabled={mutation.isPending} value={mode} onChange={e => { setMode(e.target.value as typeof mode); changed() }}><option value="now">Effective immediately</option><option value="scheduled">Schedule a price change</option><option value="correction">Correct historical prices</option></select></label>
        {mode !== 'now' && <label>{mode === 'correction' ? 'Correction start' : 'Effective from'} (local time)<input className="field-input" disabled={mutation.isPending} required type="datetime-local" value={start} onChange={e => { setStart(e.target.value); changed() }} /></label>}
        {mode === 'correction' && <label>Correction end, exclusive (local time)<input className="field-input" disabled={mutation.isPending} required type="datetime-local" value={end} onChange={e => { setEnd(e.target.value); changed() }} /></label>}
        <label>Reason<textarea className="field-textarea" aria-label="Reason" disabled={mutation.isPending} required maxLength={1000} value={reason} onChange={e => { setReason(e.target.value); changed() }} placeholder="Explain this change for the audit history" /></label>
        <p className="ux-context">Original recorded costs are retained. New revisions never overwrite pricing history.</p>
        {preview && <section className="pricing-preview" aria-label="Correction preview"><h3>Review historical correction</h3><p>{preview.events} events · {preview.previously_unpriced} previously unpriced</p><p>Existing ${preview.before_cost_usd} → Corrected ${preview.after_cost_usd}</p><p>Change: ${preview.delta_cost_usd}</p><small>Calculated {formatTimestamp(preview.calculated_at)}. Later arrivals are included during recalculation.</small><button type="button" className="btn btn-primary" onClick={() => mutation.mutate('correct')} disabled={mutation.isPending}>Confirm correction</button></section>}
        <footer className="pricing-actions"><button type="button" className="btn btn-secondary" onClick={() => setConfirmArchive(true)}>{current.availability === 'archived' ? 'Restore model' : 'Archive model'}</button><button className="btn btn-primary" disabled={mutation.isPending || ['queued', 'running'].includes(job.data?.state ?? '')}>{mutation.isPending ? 'Saving…' : mode === 'correction' ? 'Preview correction' : 'Save pricing'}</button></footer>
        {confirmArchive && <section className="pricing-preview"><h3>{current.availability === 'archived' ? 'Restore this model?' : 'Archive this model?'}</h3><p>Archiving blocks new use. Admitted runs can finish, and all usage and pricing history remain available. Restoration still requires the deployment allowlist.</p><p>The reason entered above will be saved with this action.</p><div className="dialog-actions"><button type="button" className="btn btn-ghost btn-sm" onClick={() => setConfirmArchive(false)}>Cancel</button><button type="button" className="btn btn-primary btn-sm" disabled={!reason.trim() || mutation.isPending} onClick={() => mutation.mutate('archive')}>Confirm {current.availability === 'archived' ? 'restore' : 'archive'}</button></div></section>}
      </form>}
    </section>
    <section hidden={tab !== 'history'} id="model-pricing-panel-history" role="tabpanel" aria-labelledby="model-pricing-tab-history">
      {tab === 'history' && <><h3>Immutable pricing revisions</h3>{revisions.isPending && <Skeleton label="Loading history…" rows={3} />}{revisions.error && <ProblemCard title="Pricing history unavailable" message={revisions.error.message} onRetry={() => void revisions.refetch()} />}{revisions.data && (revisions.data.items.length === 0 ? <StateCard kind="empty" compact title="No pricing revisions recorded" action={null} /> : revisions.data.items.map(revision => <article className="pricing-revision" key={revision.id}><h4>Revision {revision.number} · {revision.published_at ? 'Published' : 'Pending'}</h4><p>{revision.reason}</p><small>{revision.creator_name ?? 'System'} · {formatTimestamp(revision.created_at)}</small>{revision.intervals.map(i => <p key={i.id}>{formatTimestamp(i.effective_from)} → {i.effective_to ? formatTimestamp(i.effective_to) : 'Ongoing'}<br />{fields.map(([key, label]) => `${label}: $${i[key]}`).join(' · ')}</p>)}</article>))}<footer className="pricing-pagination"><button type="button" className="btn btn-secondary btn-sm" disabled={!historyOffset} onClick={() => setHistoryOffset(Math.max(0, historyOffset-20))}>Previous</button><button type="button" className="btn btn-secondary btn-sm" disabled={historyOffset+20 >= (revisions.data?.total ?? 0)} onClick={() => setHistoryOffset(historyOffset+20)}>Next</button></footer></>}
    </section>
    <section hidden={tab !== 'usage'} id="model-pricing-panel-usage" role="tabpanel" aria-labelledby="model-pricing-tab-usage">
      {tab === 'usage' && <UsageHistory modelId={model.id} />}
    </section>
  </Dialog>
}
