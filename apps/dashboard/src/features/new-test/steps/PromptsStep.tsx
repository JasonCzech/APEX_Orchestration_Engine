import { useEffect, useMemo, useRef, useState } from 'react'
import { useQueries } from '@tanstack/react-query'
import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { usePromptList, type PromptSummary } from '@/api/hooks/usePrompts'
import { PROMPT_LABELS, emptyPromptSelections, parsePromptSelections, type PromptSelection, type DraftPromptSelections } from '@/features/prompts/promptCategories'
import { PHASE_LABELS } from '@/features/runs/runDisplay'
import { PromptEditor } from '@/features/prompts/PromptEditor'
import type { StepProps } from '../NewRunWizard'
import { isRecord, selectedPhases } from '../wizardState'

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
  const previousScope = useRef(draft.scope)
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
    if (JSON.stringify(next) !== JSON.stringify(draft.prompt_selections) || JSON.stringify([...removed]) !== JSON.stringify(draft.prompt_override_removals)) {
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
  for (const [index, item] of allSelections.entries()) {
    const row = rows.find(row => row.id === item.prompt_id)
    if (catalog.isSuccess && (!row || row.archived_at || !row.selection_enabled)) issues.push('A selected prompt is unavailable. Choose a replacement.')
    if (versions[index]?.isPending) issues.push('Loading selected prompt versions…')
    if (versions[index]?.isError) issues.push('A selected version could not be loaded. Retry or choose a replacement.')
    if ((item.override_content ?? versions[index]?.data?.content)?.trim() === '') issues.push('Prompt content must not be blank.')
    if (row?.namespace === 'application' && !row.is_shared && !appIds.has(row.application_id)) issues.push('An application context does not match this test.')
    if (row?.namespace === 'environment' && !envIds.has(row.environment_id)) issues.push('An environment context does not match this test.')
  }
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
    return <div className="wizard-prompt-block" key={item.prompt_id}>
      <p><strong>{row?.key ?? 'Unavailable prompt'}</strong> · v{version?.data?.version ?? item.version_id}{item.override_content !== undefined ? ' · test override' : ''}</p>
      {row?.description && <p>{row.description}</p>}
      {row?.active_version && row.active_version.id !== item.version_id && <button type="button" className="btn btn-secondary btn-sm" onClick={() => update({ ...item, version_id: row.active_version!.id })}>New version available — use v{row.active_version.version}</button>}
      {version?.isPending ? <p role="status">Loading selected version…</p> : version?.isError ? <p role="alert">Selected version unavailable. <button type="button" onClick={() => void version.refetch()}>Retry</button></p> : item.override_content !== undefined ? <>
        <PromptEditor value={item.override_content} onChange={value => update({ ...item, override_content: value })} ariaLabel={`Override ${row?.key ?? 'prompt'}`} />
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => update({ prompt_id: item.prompt_id, version_id: item.version_id })}>Revert to selected version</button>
      </> : <>
        <details><summary>Preview selected content</summary><pre style={{ whiteSpace: 'pre-wrap' }}>{version?.data?.content}</pre></details>
        <button type="button" className="btn btn-ghost btn-sm" disabled={!version?.data} onClick={() => update({ ...item, override_content: version?.data?.content ?? '' })}>Override for this test</button>
      </>}
    </div>
  }
  function single(label: string, options: PromptSummary[], item: PromptSelection | null | undefined, update: (item: PromptSelection) => void) {
    return <div className="glass-panel wizard-prompt-block" key={label}>
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
    return <section className="glass-panel wizard-prompt-block" aria-label={PROMPT_LABELS[category]}>
      <h3>{PROMPT_LABELS[category]} <small>(optional)</small></h3>
      <label>Add context<select className="field-input" aria-label={`Add ${PROMPT_LABELS[category]}`} value="" disabled={(category === 'environment' && ![...envIds].some(Boolean)) || items.length >= 32} onChange={event => { const row = options.find(row => row.id === event.target.value); if (row?.active_version) change(next => { next[category].push(refFor(row)) }) }}>
        <option value="">Select a prompt to add</option>
        {options.filter(row => !items.some(item => item.prompt_id === row.id)).map(row => <option key={row.id} value={row.id}>{row.key}{row.is_shared ? ' (shared)' : ''}</option>)}
      </select></label>
      {category === 'environment' && ![...envIds].some(Boolean) && <p>Select an environment to see its context prompts.</p>}
      {!items.length && <p>No context selected.</p>}
      {items.map((item, index) => <div key={item.prompt_id}>
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
  if (!phases.length) return <section className="wizard-step" aria-label="Prompts"><p>All phases are assigned to HITL. No agent prompts are required. Saved prompt edits will be restored when a phase is assigned to LangGraph.</p></section>
  return <section className="wizard-step" aria-label="Prompts">
    <p>Choose one system prompt for the pipeline and one context prompt for each LangGraph phase. Selected versions are pinned when this test launches.</p>
    {notice && <p role="status">{notice}</p>}
    {catalog.isError && <p role="alert">Prompt catalog could not be loaded. <button type="button" onClick={() => void catalog.refetch()}>Retry</button></p>}
    {single('System Prompt', activeRows.filter(row => row.namespace === 'system'), selections.system, item => change(next => { next.system = item }))}
    <h3>Phase context</h3>
    {phases.map(phase => single(`Phase context — ${PHASE_LABELS[phase]}`, activeRows.filter(row => row.namespace === 'phase' && row.phase === phase), selections.phases[phase], item => change(next => { next.phases[phase] = item })))}
    {multiple('application', activeRows.filter(row => row.namespace === 'application' && (row.is_shared || appIds.has(row.application_id))))}
    {multiple('environment', activeRows.filter(row => row.namespace === 'environment' && envIds.has(row.environment_id)))}
    {unresolved.length > 0 && <section aria-label="Legacy prompt compatibility"><h3>Legacy prompts need review</h3><p>These overrides cannot be mapped automatically. Copy any required instructions into the appropriate selected prompt override before removing the legacy entry.</p>{unresolved.map(([key, value]) => <div key={key}><strong>{key}</strong><pre style={{ whiteSpace: 'pre-wrap' }}>{isRecord(value) ? String(value['content'] ?? '') : String(value)}</pre><button type="button" onClick={() => onChange(current => ({ ...current, prompt_overrides: Object.fromEntries(Object.entries(current.prompt_overrides).filter(([name]) => name !== key)), prompt_override_removals: [...current.prompt_override_removals, key] }))}>Remove legacy override {key}</button></div>)}</section>}
  </section>
}
