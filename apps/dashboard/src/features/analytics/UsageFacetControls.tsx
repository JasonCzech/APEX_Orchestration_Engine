import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getApexClient } from '@/api/apexClient'
import { useProjectLabels } from '@/api/hooks/useProjectLabels'
import { SearchableSelect } from '@/components/ux/SearchableSelect'
import type { AnalyticsFilters } from './analyticsFilters'

export function UsageFacetControls({ filters, isAdmin, onChange }: { filters: AnalyticsFilters; isAdmin: boolean; onChange: (patch: Partial<AnalyticsFilters>) => void }) {
  const [search, setSearch] = useState('')
  const projectLabels = useProjectLabels()
  const projectName = (id: string | null | undefined) => {
    if (!id) return undefined
    const name = !projectLabels.isError ? projectLabels.data?.find(row => row.project_id === id)?.name : undefined
    return name && name !== id ? `${name} (${id})` : id
  }
  const query = { from: filters.from, to: filters.to, project: filters.project, view: filters.view ?? (isAdmin ? 'authorized' as const : 'mine' as const), q: search }
  const facets = useQuery({ queryKey: ['analytics', 'facets', query], queryFn: async () => {
    const r = await getApexClient().GET('/v1/analytics/agents/facets', { params: { query } })
    // A malformed response must surface as this control's error, never crash the page.
    if (!r.data || !Array.isArray(r.data.applications) || !Array.isArray(r.data.users)) throw new Error('Usage filter choices could not be loaded.')
    return r.data
  } })
  const selectedApp = filters.project && filters.app ? `${filters.project.length}:${filters.project}${filters.app.length}:${filters.app}` : ''
  return <>
    <SearchableSelect label="Application" value={selectedApp} options={[{ value: '', label: 'All applications' }, ...(facets.data?.applications ?? []).map(a => ({ value: a.value, label: a.label, description: projectName(a.project_id) })), ...(selectedApp && !facets.data?.applications.some(a => a.value === selectedApp) ? [{ value: selectedApp, label: filters.app! }] : [])]} onChange={value => { const option = facets.data?.applications.find(a => a.value === value); onChange({ app: option?.app_id ?? undefined, ...(option?.project_id ? { project: option.project_id } : {}), offset: undefined }) }} />
    {isAdmin && query.view === 'authorized' && <SearchableSelect label="User" value={filters.consumer_id ?? ''} options={[{ value: '', label: 'All authorized users' }, ...(facets.data?.users ?? []), ...(filters.consumer_id && !facets.data?.users.some(u => a_u(u, filters.consumer_id!)) ? [{ value: filters.consumer_id, label: filters.consumer_id }] : [])]} onChange={value => onChange({ consumer_id: value || undefined, offset: undefined })} />}
    {(facets.data?.truncated || search) && <label>Search more choices<input type="search" className="field-input" value={search} onChange={e => setSearch(e.target.value)} /></label>}
    {facets.error && <small role="status">{facets.error.message} <button type="button" className="btn btn-ghost btn-sm" onClick={() => void facets.refetch()}>Retry</button></small>}
  </>
}

function a_u(u: { value: string }, id: string): boolean {
  return u.value === id
}
