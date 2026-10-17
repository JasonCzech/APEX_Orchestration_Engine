import { useTimestampPreference } from '@/theme/preferences'
import { TableToolbar } from '@/components/ux/TableToolbar'
import { TabList } from '@/components/ux/TabList'
import { StateCard } from '@/components/ux/StateCard'
import { Skeleton } from '@/components/ux/Skeleton'
import { SearchableSelect } from '@/components/ux/SearchableSelect'
import { formatTimestamp } from '@/utils/time'
import { orchestrationStatusLabel } from '@/features/runs/runDisplay'
import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router'

import { useEvidence, type EvidencePacket } from '@/api/hooks/useContextApi'
import { useApplicationsIndex } from '@/api/hooks/useEnvironments'
import { usePipelines } from '@/api/hooks/usePipelines'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { ProblemCard } from '@/components/ProblemCard'

const DETAIL_TABS = ['content', 'details', 'provenance'] as const
type DetailTab = (typeof DETAIL_TABS)[number]

interface EvidenceFilters {
  project: string
  application: string
  thread: string
  source: string
  query: string
}

function labelOf(value: string | null | undefined, fallback = 'Not recorded') {
  if (!value) return fallback
  return value.replace(/^app[-_]/, '').replace(/[-_]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

function evidenceKey(packet: EvidencePacket, index: number) {
  return packet.id ?? `${packet.source}-${packet.ref ?? packet.title}-${index}`
}

function packetType(packet: EvidencePacket) {
  const source = packet.source.toLowerCase()
  if (packet.ref?.startsWith('/v1/artifacts/')) return 'Artifact'
  if (source.includes('jira') || source.includes('work')) return 'Ticket'
  if (source.includes('document')) return 'Document'
  if (source.includes('observ') || source.includes('elk') || source.includes('log')) return 'Observation'
  return 'Evidence'
}

function includeCurrent(options: string[], current: string) {
  return Array.from(new Set([...options, ...(current ? [current] : [])])).sort()
}

export function EvidenceTab() {
  useTimestampPreference()
  const [searchParams, setSearchParams] = useSearchParams()
  const projectFilter = searchParams.get('project') ?? ''
  const applicationFilter = searchParams.get('app') ?? ''
  const threadFilter = searchParams.get('thread') ?? ''
  const sourceFilter = searchParams.get('source') ?? ''
  const queryFilter = searchParams.get('q') ?? ''
  const filters = useMemo<EvidenceFilters>(
    () => ({
      project: projectFilter,
      application: applicationFilter,
      thread: threadFilter,
      source: sourceFilter,
      query: queryFilter,
    }),
    [applicationFilter, projectFilter, queryFilter, sourceFilter, threadFilter],
  )
  const [draft, setDraft] = useState(filters)
  const [detailTab, setDetailTab] = useState<DetailTab>('content')

  useEffect(() => setDraft(filters), [filters])

  const evidence = useEvidence(filters.project || undefined, filters.thread || undefined)
  const pipelines = usePipelines({ limit: 100 })
  const applications = useApplicationsIndex()
  const runs = useMemo(() => pipelines.data?.items ?? [], [pipelines.data?.items])
  const runById = useMemo(() => new Map(runs.map((run) => [run.thread_id, run])), [runs])
  const appNames = useMemo(() => new Map((applications.data ?? []).map((app) => [app.id, app.name])), [applications.data])
  const packets = useMemo(() => evidence.data ?? [], [evidence.data])
  const sources = useMemo(
    () => includeCurrent(Array.from(new Set(packets.map((packet) => packet.source))), filters.source),
    [filters.source, packets],
  )
  const projects = useMemo(
    () => includeCurrent(Array.from(new Set(runs.map((run) => run.project_id).filter(Boolean) as string[])), filters.project),
    [filters.project, runs],
  )
  const applicationIds = useMemo(
    () => includeCurrent(
      Array.from(new Set(runs.filter((run) => !draft.project || run.project_id === draft.project).map((run) => run.app_id).filter(Boolean) as string[])),
      filters.application,
    ),
    [draft.project, filters.application, runs],
  )
  const filteredRuns = useMemo(
    () => runs.filter((run) =>
      (!draft.project || run.project_id === draft.project)
      && (!draft.application || run.app_id === draft.application)),
    [draft.application, draft.project, runs],
  )

  const searchedPackets = useMemo(() => {
    const query = filters.query.trim().toLowerCase()
    return packets.filter((packet) => {
      const run = packet.thread_id ? runById.get(packet.thread_id) : undefined
      if (filters.source && packet.source !== filters.source) return false
      if (!query) return true
      return [packet.title, packet.summary, packet.ref, packet.source, run?.title]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(query))
    })
  }, [filters.query, filters.source, packets, runById])
  const unclassifiedPackets = filters.application ? searchedPackets.filter(packet => packet.thread_id && !runById.has(packet.thread_id)) : []
  const visiblePackets = filters.application ? searchedPackets.filter(packet => packet.thread_id && runById.get(packet.thread_id)?.app_id === filters.application) : searchedPackets

  const selectedId = searchParams.get('selected')
  const selectedIndex = visiblePackets.findIndex((packet, index) => evidenceKey(packet, index) === selectedId)
  const selectedPacket = visiblePackets[selectedIndex >= 0 ? selectedIndex : 0]
  const selectedRun = selectedPacket?.thread_id ? runById.get(selectedPacket.thread_id) : undefined
  const activeFilterCount = Object.values(filters).filter(Boolean).length

  function commit(next: EvidenceFilters, selected?: string) {
    const params = new URLSearchParams()
    if (next.project) params.set('project', next.project)
    if (next.application) params.set('app', next.application)
    if (next.thread) params.set('thread', next.thread)
    if (next.source) params.set('source', next.source)
    if (next.query) params.set('q', next.query)
    if (selected) params.set('selected', selected)
    setSearchParams(params)
  }

  function applyFilters(event: FormEvent) { event.preventDefault(); commit(draft) }
  function clearFilters() {
    const cleared = { project: '', application: '', thread: '', source: '', query: '' }
    setDraft(cleared)
    commit(cleared)
  }
  function scopeTo(next: Partial<EvidenceFilters>) {
    const scoped = { ...filters, ...next, query: '' }
    setDraft(scoped)
    commit(scoped)
  }
  function selectPacket(packet: EvidencePacket, index: number) {
    setDetailTab('content')
    commit(filters, evidenceKey(packet, index))
  }


  return (
    <>
      <form aria-label="Evidence filters" onSubmit={applyFilters}>
        <TableToolbar label="Evidence controls" className="ctx-evidence-filters glass-panel" filterCount={activeFilterCount} onClear={clearFilters}>
        <label className="ctx-filter-field"><span>Project</span><select className="field-select" aria-label="Filter by project" value={draft.project} onChange={(e) => setDraft({ ...draft, project: e.target.value, application: '', thread: '' })}><option value="">All projects</option>{projects.map((project) => <option key={project} value={project}>{labelOf(project)}</option>)}</select></label>
        <label className="ctx-filter-field"><span>Application</span><select className="field-select" aria-label="Filter by application" value={draft.application} onChange={(e) => setDraft({ ...draft, application: e.target.value, thread: '' })}><option value="">All applications</option>{applicationIds.map((appId) => <option key={appId} value={appId}>{appNames.get(appId) ?? labelOf(appId)}</option>)}</select></label>
        <SearchableSelect label="Filter by run" value={draft.thread} onChange={(thread) => setDraft({ ...draft, thread })} options={[
          { value: '', label: 'All runs' },
          ...filteredRuns.map(run => ({ value: run.thread_id, label: run.title || 'Untitled test', description: `${formatTimestamp(run.created_at)} · ${orchestrationStatusLabel(run.thread_status, run.phase_strip)} · ${run.thread_id}` })),
          ...(draft.thread && !filteredRuns.some(run => run.thread_id === draft.thread) ? [{ value: draft.thread, label: 'Title unavailable', description: draft.thread }] : []),
        ]} />
        <label className="ctx-filter-field"><span>Source</span><select className="field-select" aria-label="Filter by source" value={draft.source} onChange={(e) => setDraft({ ...draft, source: e.target.value })}><option value="">All sources</option>{sources.map((source) => <option key={source} value={source}>{labelOf(source)}</option>)}</select></label>
        <label className="ctx-filter-field ctx-evidence-search"><span>Search evidence</span><input type="search" className="field-input" aria-label="Search evidence" placeholder="Title, reference, or finding" value={draft.query} onChange={(e) => setDraft({ ...draft, query: e.target.value })} /></label>
        <div className="ctx-evidence-filter-actions"><button type="button" className="btn btn-ghost btn-sm" onClick={clearFilters} disabled={!activeFilterCount && !Object.values(draft).some(Boolean)}>Clear</button><button type="submit" className="btn btn-secondary btn-sm">Apply filters</button></div>
        </TableToolbar>
      </form>

      {evidence.isError && evidence.data && <CachedDataWarning error={evidence.error} onRetry={() => void evidence.refetch()} />}
      {(pipelines.isError || applications.isError) && (
        <div className="ctx-inline-error" role="status">
          <span>Some association names are unavailable. Evidence remains accessible and recorded IDs are shown.</span>
          <button className="btn btn-ghost btn-sm" onClick={() => { void pipelines.refetch(); void applications.refetch() }}>Retry</button>
        </div>
      )}

      {unclassifiedPackets.length > 0 && <StateCard kind="stale" compact title={`${unclassifiedPackets.length} evidence ${unclassifiedPackets.length === 1 ? 'item has' : 'items have'} unverified application membership`}
        action={<button className="btn btn-secondary" onClick={() => { const next = { ...filters, application: '' }; setDraft(next); commit(next) }}>View evidence without application filter</button>}>
        Run details are unavailable for these linked items. The association lookup covers up to the 100 most recent accessible runs; these items are excluded from verified application matches.
      </StateCard>}
      {evidence.isPending ? (
        <Skeleton label="Loading evidence…" rows={5} />
      ) : evidence.isError && !evidence.data ? (
        <ProblemCard title="Evidence unavailable" message={evidence.error.message} onRetry={() => void evidence.refetch()} />
      ) : visiblePackets.length === 0 ? (
        <StateCard kind={activeFilterCount ? 'filtered' : 'empty'} title={unclassifiedPackets.length ? 'No verified evidence matches these filters' : activeFilterCount ? 'No evidence matches these filters' : 'No evidence yet'} action={activeFilterCount > 0 ? <button className="btn btn-secondary btn-sm" onClick={clearFilters}>Clear filters</button> : <Link to="/runs">View tests</Link>}><p>{activeFilterCount ? 'Adjust the investigation scope or clear filters to see all available evidence.' : 'Evidence accrues automatically as tests run. Project, application, and test associations will appear here.'}</p></StateCard>
      ) : (
        <section className="ctx-evidence-workspace glass-panel" aria-label="Evidence investigation workspace">
          <aside className="ctx-evidence-scope" aria-label="Evidence scope">
            <div className="ctx-pane-heading"><div><span className="ctx-pane-kicker">Scope</span><h2>Projects</h2></div><span className="ctx-count">{visiblePackets.length}</span></div>
            <nav className="ctx-scope-tree" aria-label="Project and test scope">
              <button className={!filters.project ? 'active' : ''} onClick={() => scopeTo({ project: '', application: '', thread: '' })}><span>All evidence</span><span>{packets.length}</span></button>
              {projects.map((project) => {
                const projectRuns = runs.filter((run) => run.project_id === project)
                const linkedCount = packets.filter((packet) => projectRuns.some((run) => run.thread_id === packet.thread_id)).length
                const expanded = filters.project === project || projects.length === 1
                return <div className="ctx-scope-group" key={project}>
                  <button className={filters.project === project && !filters.application && !filters.thread ? 'active' : ''} onClick={() => scopeTo({ project, application: '', thread: '' })}><span className="ctx-tree-primary">{labelOf(project)}</span><span>{linkedCount}</span></button>
                  {expanded && Array.from(new Set(projectRuns.map((run) => run.app_id).filter(Boolean) as string[])).map((appId) => {
                    const appRuns = projectRuns.filter((run) => run.app_id === appId)
                    const evidenceRuns = appRuns.filter((run) => packets.some((packet) => packet.thread_id === run.thread_id))
                    const appEvidenceCount = packets.filter((packet) => appRuns.some((run) => run.thread_id === packet.thread_id)).length
                    return <div className="ctx-scope-branch" key={appId}>
                      <button className={filters.application === appId && !filters.thread ? 'active' : ''} onClick={() => scopeTo({ project, application: appId, thread: '' })}><span>{appNames.get(appId) ?? labelOf(appId)}</span><span>{appEvidenceCount}</span></button>
                      {evidenceRuns.map((run) => <button key={run.thread_id} className={`ctx-scope-run ${filters.thread === run.thread_id ? 'active' : ''}`} onClick={() => scopeTo({ project, application: appId, thread: run.thread_id })}><span className={`ctx-run-dot ${run.execution_result ?? 'not_assessed'}`} aria-hidden="true" /><span>{run.title ?? run.thread_id}</span></button>)}
                    </div>
                  })}
                </div>
              })}
              <div className="ctx-scope-note"><strong>Project-wide / unlinked</strong><span>{packets.filter((packet) => !packet.thread_id).length}</span></div>
            </nav>
          </aside>

          <div className="ctx-evidence-list">
            <div className="ctx-pane-heading ctx-list-heading"><div><span className="ctx-pane-kicker">Results</span><h2>Evidence</h2></div><span className="ctx-caption">{visiblePackets.length} item{visiblePackets.length === 1 ? '' : 's'}</span></div>
            <div className="ctx-evidence-columns" aria-hidden="true"><span>Evidence</span><span>Source</span><span>Test</span><span>Updated</span></div>
            <div className="ctx-evidence-rows" role="listbox" aria-label="Evidence results">
              {visiblePackets.map((packet, index) => {
                const run = packet.thread_id ? runById.get(packet.thread_id) : undefined
                const packetId = evidenceKey(packet, index)
                const selected = selectedPacket === packet
                return <button key={packetId} role="option" className={`ctx-evidence-row ${selected ? 'selected' : ''}`} onClick={() => selectPacket(packet, index)} aria-selected={selected} tabIndex={selected ? 0 : -1} onKeyDown={event => {
                  const next = event.key === 'ArrowDown' ? (index + 1) % visiblePackets.length : event.key === 'ArrowUp' ? (index + visiblePackets.length - 1) % visiblePackets.length : event.key === 'Home' ? 0 : event.key === 'End' ? visiblePackets.length - 1 : -1
                  if (next < 0) return
                  event.preventDefault()
                  selectPacket(visiblePackets[next]!, next)
                  event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="option"]')[next]?.focus()
                }} data-testid={packet.id ? `evidence-${packet.id}` : undefined}>
                  <span className="ctx-evidence-primary"><strong>{packet.title}</strong><small>{packetType(packet)} · {packet.ref ?? 'No external reference'}</small></span>
                  <span><span className="dash-context-chip">{labelOf(packet.source)}</span></span>
                  <span className="ctx-row-test"><strong>{run?.title ?? (packet.thread_id ? packet.thread_id : 'Project-wide')}</strong><small>{run ? `${labelOf(run.project_id)} · ${appNames.get(run.app_id ?? '') ?? labelOf(run.app_id)}` : packet.thread_id ? 'Run details unavailable' : 'No test association'}</small></span>
                  <span className="ctx-row-meta"><span>{formatTimestamp(run?.updated_at)}</span><small className={packet.thread_id ? 'ctx-linked' : 'ctx-unlinked'}>{packet.thread_id ? 'Linked' : 'Unlinked'}</small></span>
                </button>
              })}
            </div>
          </div>

          <aside className="ctx-evidence-detail" aria-label="Artifact details">
            {selectedPacket && <>
              <div className="ctx-pane-heading ctx-detail-heading"><div><span className="ctx-pane-kicker">Artifact details</span><h2>{selectedPacket.title}</h2></div></div>
              <div className="ctx-detail-summary"><span className="dash-context-chip">{packetType(selectedPacket)}</span><span className={`status-badge ${selectedPacket.thread_id ? 'success' : 'neutral'}`}>{selectedPacket.thread_id ? 'Linked' : 'Unlinked'}</span></div>
              <div className="ctx-detail-actions">{selectedPacket.thread_id && <Link className="btn btn-secondary btn-sm" to={`/runs/${encodeURIComponent(selectedPacket.thread_id)}`}>Open {selectedRun?.title ?? 'run'}</Link>}{selectedPacket.ref && /^https?:\/\//.test(selectedPacket.ref) && <a className="btn btn-ghost btn-sm" href={selectedPacket.ref} target="_blank" rel="noreferrer">Open source</a>}</div>
              <TabList id="evidence" label="Evidence details" items={DETAIL_TABS.map(id => ({ id, label: labelOf(id) }))} value={detailTab} onChange={setDetailTab} />
              <section hidden={detailTab !== 'content'} id="evidence-panel-content" role="tabpanel" aria-labelledby="evidence-tab-content" className="ctx-detail-panel"><h3>Finding</h3><p>{selectedPacket.summary ?? 'No summary was recorded for this evidence packet.'}</p>{selectedPacket.ref && <div className="ctx-reference"><span>Reference</span><code>{selectedPacket.ref}</code></div>}</section>
              <div hidden={detailTab !== 'details'} id="evidence-panel-details" role="tabpanel" aria-labelledby="evidence-tab-details"><dl className="ctx-detail-list"><div><dt>Evidence ID</dt><dd>{selectedPacket.id ?? 'Not recorded'}</dd></div><div><dt>Type</dt><dd>{packetType(selectedPacket)}</dd></div><div><dt>Source</dt><dd>{labelOf(selectedPacket.source)}</dd></div><div><dt>Reference</dt><dd>{selectedPacket.ref ?? 'Not recorded'}</dd></div></dl></div>
              <div hidden={detailTab !== 'provenance'} id="evidence-panel-provenance" role="tabpanel" aria-labelledby="evidence-tab-provenance"><dl className="ctx-detail-list"><div><dt>Project</dt><dd>{selectedRun ? labelOf(selectedRun.project_id) : filters.project ? labelOf(filters.project) : selectedPacket.thread_id ? 'Run details unavailable' : 'Not recorded'}</dd></div><div><dt>Application</dt><dd>{selectedRun ? appNames.get(selectedRun.app_id ?? '') ?? labelOf(selectedRun.app_id) : selectedPacket.thread_id ? 'Run details unavailable' : 'Not recorded'}</dd></div><div><dt>Test</dt><dd>{selectedRun?.title ?? selectedPacket.thread_id ?? 'Not associated'}</dd></div><div><dt>Run ID</dt><dd>{selectedPacket.thread_id ?? 'Not recorded'}</dd></div><div><dt>Last run update</dt><dd>{formatTimestamp(selectedRun?.updated_at)}</dd></div></dl></div>
            </>}
          </aside>
        </section>
      )}
    </>
  )
}
