/**
 * /test-presets — every preset you can use, grouped by the application it is
 * for (F-15), with search and filters (F-11), usage, and ways to create one
 * (F-10). /test-presets/team/:presetId — a team or personal preset's page.
 */
import { useMemo } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'

import { usePresetUsage, usePresets, useTeamPreset } from '@/api/hooks/useTestPresets'
import { useConsumer } from '@/auth/AuthProvider'
import { canLaunchPipelines } from '@/auth/RequireRole'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { ProblemCard } from '@/components/ProblemCard'
import { Skeleton } from '@/components/ux/Skeleton'
import { StateCard } from '@/components/ux/StateCard'
import { TableToolbar } from '@/components/ux/TableToolbar'
import { useTimestampPreference } from '@/theme/preferences'
import { formatRelative, formatTimestamp } from '@/utils/time'

import { PresetBadge } from './PresetParts'
import { PresetDetail } from './PresetDetail'
import { engineLabel, phasesLabel, presetDraft, presetHref, reviewLabel, systemsLabel, type Preset } from './presetModel'
import { useFieldNames } from './useFieldNames'
import '@/features/golden-configs/golden-configs.css'
import './presets.css'

const VISIBILITY_FILTERS = [
  { id: '', label: 'Anyone' },
  { id: 'mine', label: 'Mine' },
  { id: 'project', label: 'Team' },
  { id: 'personal', label: 'Just me' },
  { id: 'organization', label: 'Organization' },
] as const

export function PresetsListPage() {
  useTimestampPreference()
  const [params, setParams] = useSearchParams()
  const q = params.get('q') ?? ''
  const project = params.get('project') ?? ''
  const application = params.get('application') ?? ''
  const visibility = params.get('visibility') ?? ''
  const engine = params.get('engine') ?? ''
  const showArchived = params.get('archived') === '1'
  const presets = usePresets({ includeArchived: showArchived })
  const usage = usePresetUsage(presets.data ?? [])
  const names = useFieldNames()
  const consumer = useConsumer()
  const canCreate = canLaunchPipelines(consumer)

  function setParam(key: string, value: string) {
    setParams(previous => {
      const next = new URLSearchParams(previous)
      if (value) next.set(key, value)
      else next.delete(key)
      return next
    }, { replace: true })
  }
  function clearFilters() {
    setParams(previous => {
      const next = new URLSearchParams(previous)
      for (const key of ['q', 'project', 'application', 'visibility', 'engine']) next.delete(key)
      return next
    }, { replace: true })
  }

  const all = useMemo(() => presets.data ?? [], [presets.data])
  const rows = all.filter(preset => {
    const draft = presetDraft(preset)
    if (!showArchived && preset.archived) return false
    if (project && preset.projectId !== project) return false
    if (application && (application === 'general' ? preset.applicationId !== null : preset.applicationId !== application)) return false
    if (visibility === 'mine' ? !preset.mine : visibility && preset.visibility !== visibility) return false
    if (engine && (!preset.sections.execution || draft.config.engine !== engine)) return false
    const needle = q.trim().toLowerCase()
    return !needle || `${preset.name} ${preset.description ?? ''} ${names.applicationName(preset.applicationId)}`.toLowerCase().includes(needle)
  })
  const groups = [...new Set(rows.map(preset => preset.applicationId))]
    .sort((a, b) => (a === null ? 1 : b === null ? -1 : names.applicationName(a).localeCompare(names.applicationName(b))))
    .map(app => ({ app, presets: rows.filter(preset => preset.applicationId === app).sort((a, b) => a.name.localeCompare(b.name)) }))
  const filterCount = [q.trim(), project, application, visibility, engine].filter(Boolean).length
  const projects = [...new Set(all.map(preset => preset.projectId).filter((id): id is string => Boolean(id)))].sort()
  const applications = [...new Set(all.map(preset => preset.applicationId).filter((id): id is string => Boolean(id)))]
    .sort((a, b) => names.applicationName(a).localeCompare(names.applicationName(b)))

  return (
    <section className="gc-page preset-page animate-enter">
      <header className="glass-panel preset-toolbar">
        <TableToolbar label="Preset filters" filterCount={filterCount} onClear={clearFilters}>
          <label className="ux-field">Search presets
            <input type="search" className="field-input" placeholder="Name, description or application" value={q} onChange={event => setParam('q', event.target.value)} />
          </label>
          <label className="ux-field">Application
            <select className="field-select" value={application} onChange={event => setParam('application', event.target.value)}>
              <option value="">All applications</option>
              <option value="general">General (any application)</option>
              {applications.map(id => <option key={id} value={id}>{names.applicationName(id)}</option>)}
            </select>
          </label>
          <label className="ux-field">Project
            <select className="field-select" value={project} onChange={event => setParam('project', event.target.value)}>
              <option value="">All projects</option>
              {projects.map(id => <option key={id} value={id}>{names.project(id)}</option>)}
            </select>
          </label>
          <label className="ux-field">Who can use it
            <select className="field-select" value={visibility} onChange={event => setParam('visibility', event.target.value)}>
              {VISIBILITY_FILTERS.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}
            </select>
          </label>
          <label className="ux-field">Engine
            <select className="field-select" value={engine} onChange={event => setParam('engine', event.target.value)}>
              <option value="">Any engine</option>
              <option value="sim">Simulated</option>
              <option value="apex_load">APEX Load</option>
              <option value="loadrunner">LoadRunner</option>
            </select>
          </label>
          <label className="env-archived-toggle">
            <input type="checkbox" checked={showArchived} onChange={event => setParam('archived', event.target.checked ? '1' : '')} />
            Show archived presets
          </label>
        </TableToolbar>
        {canCreate && <Link className="btn btn-primary btn-sm" to="/runs/new">New preset</Link>}
      </header>
      <p className="preset-muted preset-page-hint">
        Presets fill the new-test wizard. Create one by configuring a test and choosing <strong>Save as preset</strong> on Review, or from a test's page.
      </p>
      {presets.partialError && <CachedDataWarning error={presets.partialError} onRetry={presets.refetch} />}

      {presets.isPending ? (
        <Skeleton label="Loading test presets" rows={3} />
      ) : presets.isError ? (
        <ProblemCard title="Test presets unavailable" message="Test presets could not be loaded. Try again, or check the system status if the problem continues." onRetry={presets.refetch} />
      ) : all.length === 0 ? (
        <StateCard kind="empty" title="No test presets yet"
          action={canCreate ? <><Link className="btn btn-primary" to="/runs/new">Create a preset</Link> <Link className="btn btn-secondary" to="/runs">Save a recent test as a preset</Link></> : null}>
          <p>Configure a test in the wizard and choose Save as preset on Review, or open a test and choose Save setup as preset.</p>
        </StateCard>
      ) : rows.length === 0 ? (
        <StateCard kind="filtered" title="No matching presets" action={<button type="button" className="btn btn-secondary" onClick={clearFilters}>Show all presets</button>}>
          <p>No presets match these filters.</p>
        </StateCard>
      ) : (
        groups.map(group => (
          <section key={group.app ?? 'general'} className="glass-panel preset-table-group" aria-label={group.app ? `Presets for ${names.applicationName(group.app)}` : 'General presets'}>
            <h2 className="preset-group-title">{group.app ? names.applicationName(group.app) : 'General (any application)'} <span className="preset-muted">{group.presets.length}</span></h2>
            <div className="data-table-wrap">
              <table className="data-table striped preset-table">
                <thead>
                  <tr><th>Name</th><th>Systems</th><th>Engine</th><th>Phases</th><th>Reviews</th><th>Who can use it</th><th>Last used</th><th className="num">Tests</th></tr>
                </thead>
                <tbody>
                  {group.presets.map(preset => <PresetTableRow key={preset.key} preset={preset} names={names} stats={usage.data?.get(preset.key)} />)}
                </tbody>
              </table>
            </div>
          </section>
        ))
      )}
    </section>
  )
}

function PresetTableRow({ preset, names, stats }: {
  preset: Preset
  names: ReturnType<typeof useFieldNames>
  stats?: { tests: number; lastUsedAt: string | null }
}) {
  const draft = presetDraft(preset)
  const dash = <span className="preset-muted">—</span>
  return (
    <tr data-testid={`preset-row-${preset.key}`}>
      <td>
        <Link className="preset-row-name" to={presetHref(preset.kind, preset.id)}>{preset.name}</Link>
        {preset.description && <div className="preset-row-description">{preset.description}</div>}
      </td>
      <td>{preset.sections.systems ? systemsLabel(draft, names) : dash}</td>
      <td>{preset.sections.execution ? engineLabel(draft) : dash}</td>
      <td>{preset.sections.phases ? phasesLabel(draft).replace(/ \(.*\)$/, '') : dash}</td>
      <td>{preset.sections.phases ? reviewLabel(draft) : dash}</td>
      <td><PresetBadge preset={preset} />{preset.owner && preset.visibility !== 'organization' && <div className="preset-muted">{preset.owner}</div>}</td>
      <td>{stats?.lastUsedAt ? <span title={formatTimestamp(stats.lastUsedAt)}>{formatRelative(stats.lastUsedAt)}</span> : dash}</td>
      <td className="num">{stats?.tests ?? 0}</td>
    </tr>
  )
}

export function TeamPresetDetailPage() {
  useTimestampPreference()
  const { presetId = '' } = useParams()
  const query = useTeamPreset(presetId)
  if (query.isPending) return <Skeleton label="Loading test preset" rows={2} />
  if (!query.data) {
    return <ProblemCard title="Test preset unavailable" message={query.error?.message ?? 'This test preset could not be loaded.'} onRetry={() => void query.refetch()} />
  }
  return <PresetDetail key={presetId} preset={query.data} />
}
