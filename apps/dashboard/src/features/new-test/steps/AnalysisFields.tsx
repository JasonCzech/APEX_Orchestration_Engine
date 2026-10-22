import { useAnalysisConnections } from '@/api/hooks/useAnalysisConnections'
import { allowedAnalysisTools, ANALYSIS_LABELS, selectedPhases, type AnalysisSelection } from '../wizardState'
import type { StepProps } from '../NewRunWizard'

export function AnalysisFields({ draft, onChange }: StepProps) {
  const selections = draft.config.analysis?.selections ?? []
  const allowed = allowedAnalysisTools(draft.config.engine)
  const choices = useAnalysisConnections(draft.scope.project_id, draft.config.engine !== 'sim')
  const trafficProject = draft.scope.test_scope?.participants.find(p => p.id === draft.scope.test_scope?.traffic_entry_participant_id)?.project_id ?? draft.scope.project_id
  const nativeChoices = useAnalysisConnections(trafficProject, draft.config.engine !== 'sim')
  const update = (next: AnalysisSelection[]) => onChange(prev => ({ ...prev, config: { ...prev.config, analysis: { ...prev.config.analysis, schema_version: 1, selections: next } } }))
  if (draft.config.engine === 'sim' && !selections.length) return null
  return <fieldset className="wizard-field"><legend>Reporting &amp; Analysis tools</legend>
    <p className="wizard-caption">{selectedPhases(draft.config).includes('reporting') ? 'The Reporting agent runs these tools and assembles their results.' : 'The person completing Reporting must supply result evidence from each selected tool.'}</p>
    {choices.isError && <p role="alert">{choices.error.message}</p>}
    {[...new Set([...allowed, ...selections.map(s => s.tool)])].map(tool => {
      const selected = selections.filter(s => s.tool === tool)
      const compatible = allowed.includes(tool)
      const options = ((tool === 'internal_rca' ? nativeChoices : choices).data ?? []).filter(c => tool === 'internal_rca' ? c.kind === 'execution_engine' && c.provider === 'apex_load' : c.kind === 'analysis' && c.provider === tool)
      return <div key={tool}><label><input type="checkbox" checked={selected.length > 0} disabled={!compatible && !selected.length} onChange={e => update(e.target.checked ? [...selections, { tool, connection_id: '' }] : selections.filter(s => s.tool !== tool))} />{ANALYSIS_LABELS[tool]}</label>
        {!compatible && <p role="alert">Incompatible with {draft.config.engine}. Remove this selection.</p>}
        {selected.length > 0 && <label>{ANALYSIS_LABELS[tool]} connector{tool === 'other' ? 's' : ''}<select aria-label={`${ANALYSIS_LABELS[tool]} connector${tool === 'other' ? 's' : ''}`} className="field-input" multiple={tool === 'other'} value={tool === 'other' ? selected.map(s => s.connection_id) : selected[0]?.connection_id ?? ''} onChange={e => update([...selections.filter(s => s.tool !== tool), ...Array.from(e.target.selectedOptions, o => ({ tool, connection_id: o.value }))])}>
          {tool !== 'other' && <option value="">Choose a connector</option>}
          {selected.filter(s => s.connection_id && !options.some(o => o.id === s.connection_id)).map(s => <option key={s.connection_id} value={s.connection_id}>Unavailable: {s.connection_id}</option>)}
          {options.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select></label>}
      </div>
    })}
  </fieldset>
}
