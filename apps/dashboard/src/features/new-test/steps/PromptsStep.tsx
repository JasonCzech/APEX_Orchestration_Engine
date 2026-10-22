import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'
import { useQueries } from '@tanstack/react-query'
import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { usePromptList, type PromptSummary } from '@/api/hooks/usePrompts'
import { PROMPT_LABELS, emptyPromptSelections, parsePromptSelections, type PromptSelection, type DraftPromptSelections } from '@/features/prompts/promptCategories'
import { PHASE_LABELS } from '@/features/runs/runDisplay'
import { PromptEditor } from '@/features/prompts/PromptEditor'
import type { StepProps } from '../NewRunWizard'
import { isRecord, selectedPhases } from '../wizardState'

const LOADING_VERSIONS = 'Loading selected prompt versions…'

type PromptTabId = 'system' | 'application' | 'environment' | `phase:${PhaseName}`
type PromptTabStatus = 'ok' | 'attention' | 'pending' | 'empty'
interface PromptTab {
  id: PromptTabId
  label: string
  group: 'pipeline' | 'phase' | 'context'
  status: PromptTabStatus
  summary: string
  problems: string[]
  overridden: boolean
}
const STATUS_GLYPHS: Partial<Record<PromptTabStatus, string>> = { ok: '✓', attention: '!', pending: '…' }
const STATUS_TEXT: Partial<Record<PromptTabStatus, string>> = { attention: 'Needs attention', pending: 'Loading' }
const tabKey = (id: PromptTabId) => id.replace(':', '-')

function refFor(row: PromptSummary): PromptSelection {
  return { prompt_id: row.id, version_id: row.active_version!.id }
}

export function PromptsStep({ draft, onChange, onNormalize = onChange }: StepProps) {
  const catalog = usePromptList({ selection_enabled: true })
  const appIds = new Set<string | null | undefined>(draft.scope.test_scope?.participants.map(p => p.application_id) ?? [draft.scope.app_id])
  const envIds = new Set<string | null | undefined>(draft.scope.test_scope?.participants.map(p => p.environment_id) ?? [draft.scope.environment_id])
  const rows = catalog.data ?? []
  const phases = selectedPhases(draft.config)
  const selections = draft.prompt_selections ?? emptyPromptSelections()
  const [notice, setNotice] = useState('')
  const [activeTab, setActiveTab] = useState<PromptTabId | null>(null)
  const tabsId = useId()
  const previousScope = useRef(draft.scope)
  // True once a normalization pass over the loaded catalog had nothing left to change.
  const normalizedRef = useRef(false)
  const legacy = useMemo(() => {
    const inherited = draft.config.golden_configurable?.['prompt_overrides']
    return { ...(isRecord(inherited) ? inherited : {}), ...draft.prompt_overrides }
  }, [draft.config.golden_configurable, draft.prompt_overrides])
  const allSelections = (phases.length ? [selections.system, ...phases.map(phase => selections.phases[phase]), ...selections.application, ...selections.environment] : []).filter((item): item is PromptSelection => Boolean(item))
  const versions = useQueries({ queries: allSelections.map(item => ({
    queryKey: ['prompts', 'selected-version', item.prompt_id, item.version_id],
    queryFn: async ({ signal }: { signal: AbortSignal }) => {
      const { data, error, response } = await getApexClient().GET('/v1/prompts/{prompt_id}/versions/{version_id}', {
        params: { path: { prompt_id: item.prompt_id, version_id: item.version_id } }, signal,
      })
      if (!response.ok || !data) throw new ApiError(response.status, errorMessageOf(error, 'Selected version is unavailable'), error)
      return data
    },
    staleTime: 60_000,
  })) })
  const contentFor = (item: PromptSelection) => versions[allSelections.findIndex(row => row.prompt_id === item.prompt_id && row.version_id === item.version_id)]

  useEffect(() => {
    normalizedRef.current = false
    if (!phases.length || !catalog.data || catalog.isError || catalog.isPlaceholderData) return
    const next: DraftPromptSelections = structuredClone(draft.prompt_selections ?? parsePromptSelections(draft.config.golden_configurable?.['prompt_selections']) ?? emptyPromptSelections())
    const overrides = { ...legacy }
    const removed = new Set(draft.prompt_override_removals)
    for (const key of removed) delete overrides[key]
    if (!next.system) {
      const row = rows.find(row => row.namespace === 'system' && row.is_default && row.active_version)
      if (row) next.system = refFor(row)
    }
    for (const phase of phases) {
      if (!next.phases[phase]) {
        const row = rows.find(row => row.namespace === 'phase' && row.phase === phase && row.is_default && row.active_version)
        if (row) next.phases[phase] = refFor(row)
      }
      const key = `phase/${phase}`
      if (next.phases[phase] && isRecord(overrides[key]) && typeof overrides[key]['content'] === 'string') {
        next.phases[phase] = { ...next.phases[phase]!, override_content: overrides[key]['content'] }
        removed.add(key)
        delete overrides[key]
      }
    }
    // Preserve phase context when ownership moves to HITL; launch filters agent contexts.
    const appKey = `application/${draft.scope.app_id}`
    const app = rows.find(row => row.namespace === 'application' && row.key === draft.scope.app_id && appIds.has(row.application_id) && row.active_version)
    if (app && isRecord(overrides[appKey]) && typeof overrides[appKey]['content'] === 'string') {
      if (!next.application.some(item => item.prompt_id === app.id)) next.application.push({ ...refFor(app), override_content: overrides[appKey]['content'] })
      removed.add(appKey)
      delete overrides[appKey]
    }
    let cleared = false
    if (JSON.stringify(previousScope.current) !== JSON.stringify(draft.scope)) {
      const filter = (items: PromptSelection[], category: 'application' | 'environment') => items.filter(item => {
        const row = rows.find(row => row.id === item.prompt_id)
        const keep = category === 'application' ? row?.is_shared || (row?.application_id && appIds.has(row.application_id)) : row?.environment_id && envIds.has(row.environment_id)
        if (!keep) cleared = true
        return keep
      })
      next.application = filter(next.application, 'application')
      next.environment = filter(next.environment, 'environment')
    }
    previousScope.current = draft.scope
    if (cleared) setNotice('Context selections that no longer match the application or environment were removed.')
    const unchanged = JSON.stringify(next) === JSON.stringify(draft.prompt_selections) && JSON.stringify([...removed]) === JSON.stringify(draft.prompt_override_removals)
    normalizedRef.current = unchanged
    if (!unchanged) {
      onNormalize(current => ({
        ...current,
        // Keep the old intent reconstructable until any ambiguous pre-upgrade
        // launch has been reconciled. Automatic prompt migration is not a new
        // user request and must not bypass the durable retry quarantine.
        ...(removed.size > current.prompt_override_removals.length && current.legacy_prompt_overrides === undefined
          ? { legacy_prompt_overrides: { ...current.prompt_overrides }, legacy_prompt_override_removals: [...current.prompt_override_removals] }
          : {}),
        prompt_selections: next,
        prompt_overrides: Object.fromEntries(Object.entries(current.prompt_overrides).filter(([key]) => !removed.has(key))),
        prompt_override_removals: [...removed],
      }))
    }
  }, [catalog.data, catalog.isError, catalog.isPlaceholderData, draft.prompt_selections, draft.scope, draft.config, draft.prompt_override_removals, legacy, onNormalize]) // eslint-disable-line react-hooks/exhaustive-deps

  const issues: string[] = []
  if (phases.length && catalog.isError) issues.push('Prompt catalog could not be loaded. Retry before launching.')
  else if (phases.length && (catalog.isPending || catalog.isPlaceholderData)) issues.push('Loading prompt catalog…')
  // Per selection, so each tab can show its own state as well as feed the launch issues.
  function itemIssues(item: PromptSelection): string[] {
    const found: string[] = []
    const row = rows.find(row => row.id === item.prompt_id)
    const version = contentFor(item)
    if (catalog.isSuccess && (!row || row.archived_at || !row.selection_enabled)) found.push('A selected prompt is unavailable. Choose a replacement.')
    if (version?.isPending) found.push(LOADING_VERSIONS)
    if (version?.isError) found.push('A selected version could not be loaded. Retry or choose a replacement.')
    if ((item.override_content ?? version?.data?.content)?.trim() === '') found.push('Prompt content must not be blank.')
    if (row?.namespace === 'application' && !row.is_shared && !appIds.has(row.application_id)) found.push('An application context does not match this test.')
    if (row?.namespace === 'environment' && !envIds.has(row.environment_id)) found.push('An environment context does not match this test.')
    return found
  }
  for (const item of allSelections) issues.push(...itemIssues(item))
  const unresolved = Object.entries(legacy).filter(([key]) => phases.length > 0 && !draft.prompt_override_removals.includes(key) && (!key.startsWith('phase/') || phases.some(phase => key.startsWith(`phase/${phase}`))))
  if (unresolved.length) issues.push('Resolve the retained legacy prompt overrides before launching.')
  const serializedIssues = JSON.stringify([...new Set(issues)])
  useEffect(() => {
    if (serializedIssues !== JSON.stringify(draft.prompt_selection_issues ?? [])) onNormalize(current => ({ ...current, prompt_selection_issues: JSON.parse(serializedIssues) as string[] }))
  }, [serializedIssues, draft.prompt_selection_issues, onNormalize])

  function change(update: (value: DraftPromptSelections) => void) {
    onChange(current => {
      const next = structuredClone(current.prompt_selections ?? emptyPromptSelections())
      update(next)
      return { ...current, prompt_selections: next }
    })
  }
  function preview(item: PromptSelection, update: (value: PromptSelection) => void) {
    const row = rows.find(row => row.id === item.prompt_id)
    const version = contentFor(item)
    const overridden = item.override_content !== undefined
    const ready = !version?.isPending && !version?.isError
    return <div className="wizard-prompt-block wizard-prompt-preview" key={item.prompt_id}>
      <div className="wizard-prompt-preview-head">
        <p><strong>{row?.key ?? 'Unavailable prompt'}</strong> · v{version?.data?.version ?? item.version_id}{overridden ? ' · test override' : ''}</p>
        {ready && (overridden
          ? <button type="button" className="btn btn-ghost btn-sm" onClick={() => update({ prompt_id: item.prompt_id, version_id: item.version_id })}>Revert to selected version</button>
          : <button type="button" className="btn btn-secondary btn-sm" disabled={!version?.data} onClick={() => update({ ...item, override_content: version?.data?.content ?? '' })}>Override for this test</button>)}
      </div>
      {row?.description && <p className="wizard-caption">{row.description}</p>}
      {row?.active_version && row.active_version.id !== item.version_id && <button type="button" className="btn btn-secondary btn-sm wizard-prompt-override" onClick={() => update({ ...item, version_id: row.active_version!.id })}>New version available — use v{row.active_version.version}</button>}
      {version?.isPending ? <p role="status">Loading selected version…</p> : version?.isError ? <p role="alert">Selected version unavailable. <button type="button" onClick={() => void version.refetch()}>Retry</button></p> : overridden
        ? <PromptEditor value={item.override_content!} onChange={value => update({ ...item, override_content: value })} ariaLabel={`Override ${row?.key ?? 'prompt'}`} />
        : <pre className="wizard-prompt-content" aria-label={`${row?.key ?? 'Prompt'} content`}>{version?.data?.content}</pre>}
    </div>
  }
  function single(label: string, options: PromptSummary[], item: PromptSelection | null | undefined, update: (item: PromptSelection) => void) {
    return <div className="wizard-prompt-block wizard-prompt-block--flat" key={label}>
      <label className="wizard-label">{label} (required)<select className="field-input" aria-label={label} value={item?.prompt_id ?? ''} onChange={event => { const row = options.find(row => row.id === event.target.value); if (row?.active_version) update(refFor(row)) }}>
        <option value="">Select a prompt</option>
        {item && !options.some(row => row.id === item.prompt_id) && <option value={item.prompt_id}>Unavailable selection</option>}
        {options.map(row => <option key={row.id} value={row.id}>{row.key}{row.is_default ? ' (default)' : ''}</option>)}
      </select></label>
      {!item && catalog.isSuccess && <p role="alert">{options.length ? 'No catalog default is available. Choose a prompt.' : 'No eligible prompt is available. Ask an administrator to create one.'}</p>}
      {item && preview(item, update)}
    </div>
  }
  function multiple(category: 'application' | 'environment', options: PromptSummary[]) {
    const items = selections[category]
    return <section className="wizard-prompt-block wizard-prompt-block--flat" aria-label={PROMPT_LABELS[category]}>
      <label className="wizard-label">Add context<select className="field-input" aria-label={`Add ${PROMPT_LABELS[category]}`} value="" disabled={(category === 'environment' && ![...envIds].some(Boolean)) || items.length >= 32} onChange={event => { const row = options.find(row => row.id === event.target.value); if (row?.active_version) change(next => { next[category].push(refFor(row)) }) }}>
        <option value="">Select a prompt to add</option>
        {options.filter(row => !items.some(item => item.prompt_id === row.id)).map(row => <option key={row.id} value={row.id}>{row.key}{row.is_shared ? ' (shared)' : ''}</option>)}
      </select></label>
      {category === 'environment' && ![...envIds].some(Boolean) && <p>Select an environment to see its context prompts.</p>}
      {!items.length && <p className="wizard-caption">No context selected. Context is optional and is added to every phase in the order listed.</p>}
      {items.map((item, index) => <div key={item.prompt_id} className="wizard-prompt-context-item">
        {preview(item, value => change(next => { next[category][index] = value }))}
        <div className="wizard-row">
          <button type="button" className="btn btn-ghost btn-sm" disabled={index === 0} onClick={() => change(next => { [next[category][index - 1], next[category][index]] = [next[category][index]!, next[category][index - 1]!] })}>Move up</button>
          <button type="button" className="btn btn-ghost btn-sm" disabled={index === items.length - 1} onClick={() => change(next => { [next[category][index], next[category][index + 1]] = [next[category][index + 1]!, next[category][index]!] })}>Move down</button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => change(next => { next[category].splice(index, 1) })}>Remove context</button>
        </div>
      </div>)}
    </section>
  }
  const activeRows = rows.filter(row => !row.archived_at && row.active_version && row.selection_enabled)

  // One tab per prompt slot, in the order the pipeline assembles them.
  function tabFor(id: PromptTabId, label: string, group: PromptTab['group'], items: PromptSelection[], required: boolean): PromptTab {
    const found = items.flatMap(itemIssues)
    const problems = [...new Set(found.filter(issue => issue !== LOADING_VERSIONS))]
    const keyOf = (item: PromptSelection) => rows.find(row => row.id === item.prompt_id)?.key ?? 'Unavailable prompt'
    const status: PromptTabStatus = catalog.isError || problems.length || (required && !items.length && catalog.isSuccess) ? 'attention'
      : catalog.isPending || catalog.isPlaceholderData || found.includes(LOADING_VERSIONS) ? 'pending'
      : items.length ? 'ok' : 'empty'
    const summary = required ? (items[0] ? keyOf(items[0]) : 'Not selected') : items.length ? `${items.length} selected` : 'None'
    return { id, label, group, status, summary, problems, overridden: items.some(item => item.override_content !== undefined) }
  }
  const tabs: PromptTab[] = [
    tabFor('system', 'System prompt', 'pipeline', selections.system ? [selections.system] : [], true),
    ...phases.map(phase => tabFor(`phase:${phase}`, PHASE_LABELS[phase], 'phase', selections.phases[phase] ? [selections.phases[phase]!] : [], true)),
    tabFor('application', PROMPT_LABELS.application!, 'context', selections.application, false),
    tabFor('environment', PROMPT_LABELS.environment!, 'context', selections.environment, false),
  ]
  // Open where attention is needed once defaults are applied and versions have
  // loaded, then stay where the user is: fixing one tab must not move them on.
  const firstAttention = tabs.find(tab => tab.status === 'attention')?.id
  const settled = tabs.every(tab => tab.status !== 'pending')
  useEffect(() => {
    if (activeTab === null && settled && normalizedRef.current) setActiveTab(firstAttention ?? 'system')
  }, [activeTab, firstAttention, settled])
  const active = tabs.find(tab => tab.id === activeTab) ?? tabs[0]!
  const activeIndex = tabs.indexOf(active)
  const hitlPhases = PHASE_NAMES.filter(phase => !phases.includes(phase))
  function selectTab(id: PromptTabId, focus = false) {
    setActiveTab(id)
    const tab = document.getElementById(`${tabsId}-tab-${tabKey(id)}`)
    if (focus) tab?.focus()
    // On narrow screens the rail is a horizontal strip; keep the selection in it.
    const list = tab?.parentElement
    if (list && list.scrollWidth > list.clientWidth) tab?.scrollIntoView?.({ block: 'nearest', inline: 'center' })
  }
  function moveTab(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const next = event.key === 'ArrowDown' || event.key === 'ArrowRight' ? (index + 1) % tabs.length
      : event.key === 'ArrowUp' || event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length
      : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : -1
    if (next < 0) return
    event.preventDefault()
    selectTab(tabs[next]!.id, true)
  }
  function renderTab(tab: PromptTab) {
    const index = tabs.indexOf(tab)
    const phaseNumber = tab.group === 'phase' ? tabs.filter(item => item.group === 'phase').indexOf(tab) + 1 : null
    return <button key={tab.id} type="button" role="tab" id={`${tabsId}-tab-${tabKey(tab.id)}`}
      className={`wizard-prompt-tab wizard-prompt-tab--${tab.status}`}
      aria-selected={tab.id === active.id} aria-controls={`${tabsId}-panel`} tabIndex={tab.id === active.id ? 0 : -1}
      onClick={() => selectTab(tab.id)} onKeyDown={event => moveTab(event, index)}>
      <span className="wizard-prompt-tab-marker" aria-hidden="true">{STATUS_GLYPHS[tab.status] ?? phaseNumber ?? '•'}</span>
      <span className="wizard-prompt-tab-text">
        <span className="wizard-prompt-tab-label">{tab.label}</span>
        <span className="wizard-prompt-tab-summary">{tab.summary}{tab.overridden ? ' · override' : ''}</span>
      </span>
      {STATUS_TEXT[tab.status] && <span className="sr-only">{STATUS_TEXT[tab.status]}</span>}
    </button>
  }
  const phaseTabs = tabs.filter(tab => tab.group === 'phase')
  const activePhase = active.id.startsWith('phase:') ? active.id.slice('phase:'.length) as PhaseName : null
  const panelCopy: Record<PromptTab['group'], { kicker: string; description: string }> = {
    pipeline: { kicker: 'Required · all phases', description: 'Shared instructions every LangGraph phase receives before its own context.' },
    phase: { kicker: `Required · phase ${phaseTabs.indexOf(active) + 1} of ${phaseTabs.length}`, description: `Context sent only to the ${active.label} agent.` },
    context: { kicker: 'Optional · all phases', description: active.id === 'application' ? 'Application knowledge added to every phase, in the order listed.' : 'Environment knowledge added to every phase, in the order listed.' },
  }

  if (!phases.length) return <section className="wizard-step" aria-label="Prompts"><p>All phases are assigned to HITL. No agent prompts are required. Saved prompt edits will be restored when a phase is assigned to LangGraph.</p></section>
  return <section className="wizard-step" aria-label="Prompts">
    <p className="wizard-step-hint">Choose one system prompt for the pipeline and one context prompt for each LangGraph phase. Selected versions are pinned when this test launches.</p>
    {notice && <p role="status">{notice}</p>}
    {catalog.isError && <p role="alert">Prompt catalog could not be loaded. <button type="button" onClick={() => void catalog.refetch()}>Retry</button></p>}
    {unresolved.length > 0 && <section className="tonal-card wizard-prompt-legacy" aria-label="Legacy prompt compatibility"><h3>Legacy prompts need review</h3><p>These overrides cannot be mapped automatically. Copy any required instructions into the appropriate selected prompt override before removing the legacy entry.</p>{unresolved.map(([key, value]) => <div key={key}><strong>{key}</strong><pre style={{ whiteSpace: 'pre-wrap' }}>{isRecord(value) ? String(value['content'] ?? '') : String(value)}</pre><button type="button" onClick={() => onChange(current => ({ ...current, prompt_overrides: Object.fromEntries(Object.entries(current.prompt_overrides).filter(([name]) => name !== key)), prompt_override_removals: [...current.prompt_override_removals, key] }))}>Remove legacy override {key}</button></div>)}</section>}
    <div className="wizard-prompt-layout">
      <nav className="wizard-prompt-nav" aria-label="Prompt slots">
        <div role="tablist" aria-label="Prompt selection" aria-orientation="vertical" className="wizard-prompt-tablist">
          {renderTab(tabs[0]!)}
          <span className="wizard-prompt-nav-group" aria-hidden="true">Phase context</span>
          {phaseTabs.map(renderTab)}
          <span className="wizard-prompt-nav-group" aria-hidden="true">Optional context</span>
          {tabs.filter(tab => tab.group === 'context').map(renderTab)}
        </div>
        {hitlPhases.length > 0 && <p className="wizard-caption wizard-prompt-nav-note">HITL, no prompt needed: {hitlPhases.map(phase => PHASE_LABELS[phase]).join(', ')}</p>}
      </nav>
      <div role="tabpanel" id={`${tabsId}-panel`} aria-labelledby={`${tabsId}-tab-${tabKey(active.id)}`} className="glass-panel wizard-prompt-tabpanel">
        <header className="wizard-prompt-tabpanel-head">
          <span className="wizard-section-kicker">{panelCopy[active.group].kicker}</span>
          <h3>{active.label}</h3>
          <p className="wizard-caption">{panelCopy[active.group].description}</p>
        </header>
        {active.problems.length > 0 && <ul className="wizard-hint-list">{active.problems.map(problem => <li key={problem} className="wizard-caption wizard-caption--warning">{problem}</li>)}</ul>}
        {active.id === 'system' && single('System Prompt', activeRows.filter(row => row.namespace === 'system'), selections.system, item => change(next => { next.system = item }))}
        {activePhase && single(`Phase context — ${PHASE_LABELS[activePhase]}`, activeRows.filter(row => row.namespace === 'phase' && row.phase === activePhase), selections.phases[activePhase], item => change(next => { next.phases[activePhase] = item }))}
        {active.id === 'application' && multiple('application', activeRows.filter(row => row.namespace === 'application' && (row.is_shared || appIds.has(row.application_id))))}
        {active.id === 'environment' && multiple('environment', activeRows.filter(row => row.namespace === 'environment' && envIds.has(row.environment_id)))}
        <div className="wizard-prompt-pager">
          {activeIndex > 0 && <button type="button" className="btn btn-ghost btn-sm" onClick={() => selectTab(tabs[activeIndex - 1]!.id, true)}>← {tabs[activeIndex - 1]!.label}</button>}
          <span className="wizard-footer-spacer" />
          {activeIndex < tabs.length - 1 && <button type="button" className="btn btn-ghost btn-sm" onClick={() => selectTab(tabs[activeIndex + 1]!.id, true)}>{tabs[activeIndex + 1]!.label} →</button>}
        </div>
      </div>
    </div>
  </section>
}
