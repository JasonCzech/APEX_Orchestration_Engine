import { useState } from 'react'
import type { HumanCompletionPayload } from '@apex/pipeline-events'

interface Deliverable {
  schema_version: number; phase: string; disposition: string; summary: string
  sections: Record<string, string>; evidence: string[]; assumptions: string[]; gaps: string[]; blockers: string[]
  workload: Array<{ scenario_id: string; percent: number }>
}
function BufferedText({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const [raw, setRaw] = useState(value)
  return <textarea className="field-input" value={raw} onChange={event => { setRaw(event.target.value); onChange(event.target.value) }} />
}
function BufferedNumber({ value, onChange }: { value: number | undefined; onChange: (value: number | undefined) => void }) {
  const [raw, setRaw] = useState(value === undefined ? '' : String(value))
  return <input className="field-input" inputMode="decimal" value={raw} onChange={event => { const text = event.target.value; setRaw(text); onChange(text.trim() && Number.isFinite(Number(text)) ? Number(text) : undefined) }} />
}
export function HumanCompletionPanel({ payload, output, disabled, onChange }: {
  payload: HumanCompletionPayload; output?: Record<string, unknown>; disabled: boolean
  onChange: (output: Record<string, unknown>) => void
}) {
  const deliverable = (output?.deliverable ?? { schema_version: 1, phase: payload.phase, disposition: 'complete', summary: '', sections: {}, evidence: [], assumptions: [], gaps: [], blockers: [], workload: [] }) as Deliverable
  const set = (key: string, value: unknown) => onChange({ ...output, deliverable, [key]: value })
  const edit = (key: string, value: unknown) => set('deliverable', { ...deliverable, [key]: value })
  const selected = (output?.artifact_ids ?? []) as string[]
  const external = (output?.external_results ?? { source: '', uri: '', engine: payload.engine, kpis: {} }) as { source: string; uri: string; engine: string; kpis: Record<string, number> }
  const spec = (output?.load_test_spec ?? { title: '', target_environment: '', vusers: 1, ramp_s: 0, duration_s: 60, script_refs: [], slas: {} }) as Record<string, unknown>
  return <fieldset disabled={disabled} className="wizard-field">
    <legend>Human completion</legend>
    <p>Complete the required work and supply its evidence. The workflow resumes after validation.</p>
    <label>Summary<textarea className="field-input" value={deliverable.summary} onChange={e => edit('summary', e.target.value)} /></label>
    {payload.sections.map(section => <label key={section}>{section.replaceAll('_', ' ')}<textarea className="field-input" value={deliverable.sections[section] ?? ''} onChange={e => edit('sections', { ...deliverable.sections, [section]: e.target.value })} /></label>)}
    <label>Additional evidence references (one per line)<BufferedText value={((output?.operator_evidence as string[] | undefined) ?? deliverable.evidence.filter(ref => !Object.values((output?.analysis_results ?? {}) as Record<string, string[]>).flat().includes(ref))).join('\n')} onChange={text => {
      const manual = text.split('\n').filter(Boolean)
      const toolRefs = Object.values((output?.analysis_results ?? {}) as Record<string, string[]>).flat().filter(ref => /^https?:\/\//.test(ref))
      onChange({ ...output, operator_evidence: manual, deliverable: { ...deliverable, evidence: [...new Set([...manual, ...toolRefs])] } })
    }} /></label>
    {payload.phase === 'test_planning' && <label>Workload (one scenario ID and percentage per line, separated by a comma)<BufferedText value={deliverable.workload.map(w => `${w.scenario_id},${w.percent}`).join('\n')} onChange={text => edit('workload', text.split('\n').filter(Boolean).map(line => { const [scenario_id = '', percent = ''] = line.split(','); return { scenario_id, percent: percent.trim() ? Number(percent) : null } }))} /></label>}
    {payload.requires_load_test_spec && <>
      <p>Supply the workload the execution engine will run.</p>
      {payload.approved_target && <p>Approved target: {payload.approved_target}</p>}
      {['title', ...(payload.approved_target ? [] : ['target_environment'])].map(key => <label key={key}>{key.replaceAll('_', ' ')}<input className="field-input" value={String(spec[key] ?? '')} onChange={e => set('load_test_spec', { ...spec, [key]: e.target.value })} /></label>)}
      {['vusers', 'ramp_s', 'duration_s'].map(key => <label key={key}>{key.replaceAll('_', ' ')}<input type="number" className="field-input" value={Number(spec[key] ?? 0)} onChange={e => set('load_test_spec', { ...spec, [key]: Number(e.target.value) })} /></label>)}
      <p>Scripts are selected through the approved provider workload. This form cannot override that binding.</p>
    </>}
    {payload.phase === 'execution' && <>
      {(['source', 'uri'] as const).map(key => <label key={key}>Execution {key}<input className="field-input" value={external[key]} onChange={e => set('external_results', { ...external, [key]: e.target.value })} /></label>)}
      {['p95_ms', 'tps_avg', 'error_rate'].map(key => <label key={key}>{key}<BufferedNumber value={external.kpis[key]} onChange={value => { const kpis = { ...external.kpis }; if (value === undefined) delete kpis[key]; else kpis[key] = value; set('external_results', { ...external, kpis }) }} /></label>)}
      <label>Native run ID<input className="field-input" value={String(output?.native_run_id ?? '')} onChange={e => { const next: Record<string, unknown> = { ...output, deliverable }; if (e.target.value) next.native_run_id = e.target.value; else delete next.native_run_id; onChange(next) }} /></label>
    </>}
    {payload.artifact_ids.map(id => <label key={id}><input type="checkbox" checked={selected.includes(id)} onChange={e => set('artifact_ids', e.target.checked ? [...selected, id] : selected.filter(x => x !== id))} />{id}</label>)}
    {payload.analysis_selections.map(tool => <label key={tool.connection_id}>{tool.tool} result references (artifact IDs or report URLs, one per line)<BufferedText value={(((output?.analysis_results ?? {}) as Record<string, string[]>)[tool.connection_id] ?? []).join('\n')} onChange={text => {
      const refs = text.split('\n').filter(Boolean)
      const oldRefs = Object.values((output?.analysis_results ?? {}) as Record<string, string[]>).flat()
      const allRefs = Object.values({ ...((output?.analysis_results ?? {}) as Record<string, string[]>), [tool.connection_id]: refs }).flat()
      const evidence = [...new Set([...((output?.operator_evidence as string[] | undefined) ?? deliverable.evidence.filter(ref => !oldRefs.includes(ref))), ...allRefs.filter(ref => ref.startsWith('https://') || ref.startsWith('http://'))]) ]
      onChange({ ...output, deliverable: { ...deliverable, evidence }, analysis_results: { ...(output?.analysis_results as object ?? {}), [tool.connection_id]: refs } })
    }} /></label>)}
  </fieldset>
}
