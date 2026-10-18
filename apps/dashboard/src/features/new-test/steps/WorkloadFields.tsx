import type { StepProps } from '../NewRunWizard'
import { effectiveWorkload, inputNumber, SLA_FIELDS, WORKLOAD_FIELDS, workloadIssues } from '../workload'

export function WorkloadFields({ draft, onChange }: StepProps) {
  const load = effectiveWorkload(draft.config.golden_configurable?.load_test)
  const issues = workloadIssues(draft.config.golden_configurable?.load_test)
  const nativeManaged = draft.config.engine === 'loadrunner'
  const extraSlas = Object.entries(load.slas).filter(([key]) => !SLA_FIELDS.some(field => field.key === key))
  function change(key: string, value: string, sla = false, scale = 1) {
    onChange(previous => {
      const bundle = previous.config.golden_configurable ?? {}
      const effective = effectiveWorkload(bundle.load_test)
      const next = { ...effective }
      if (sla) {
        next.slas = { ...effective.slas }
        if (value === '') delete next.slas[key]
        else next.slas[key] = Number(value) / scale
      } else next[key] = value === '' ? '' : Number(value)
      return { ...previous, config: { ...previous.config, golden_configurable: { ...bundle, load_test: next } } }
    })
  }
  function resetWorkload() {
    onChange(previous => {
      const bundle = previous.config.golden_configurable ?? {}
      const existing = bundle.load_test && typeof bundle.load_test === 'object' && !Array.isArray(bundle.load_test) ? bundle.load_test as Record<string, unknown> : {}
      return { ...previous, config: { ...previous.config, golden_configurable: { ...bundle, load_test: { ...Object.fromEntries(Object.entries(existing).filter(([key]) => ['title', 'release_id', 'mapping_revision', 'fail_at_pct', 'abortive_stop'].includes(key))), ...effectiveWorkload(undefined) } } } }
    })
  }
  return <section className="wizard-workload" aria-labelledby="workload-heading">
    <h3 id="workload-heading">Requested workload and acceptance</h3>
    <p className="wizard-caption">For Simulated and APEX Load execution, these structured inputs take precedence over workload suggestions in the request. Review the generated scenario before execution. Defaults are shown until you edit them or select a preset.</p>
    {nativeManaged && <p className="tonal-card warning">LoadRunner uses its configured native test for workload and acceptance. Duration and ramp below estimate the reservation window only; they do not replace the native test’s VUsers, pacing or SLA settings. Review those settings in LoadRunner before launch.</p>}
    <fieldset className="wizard-fieldset">
      <div className="wizard-workload-grid">
        {WORKLOAD_FIELDS.map(field => <label className="wizard-field" key={field.key} htmlFor={`workload-${field.key}`}>
          <span className="wizard-label">{nativeManaged && field.key !== 'vusers' ? `Reservation ${field.label.toLowerCase()}` : field.label}</span>
          <input className="field-input" disabled={nativeManaged && field.key === 'vusers'} id={`workload-${field.key}`} type="number" min={field.min} max={field.max} step={field.step}
            value={inputNumber(load[field.key])} onChange={event => change(field.key, event.target.value)}
            aria-invalid={issues.some(issue => issue.startsWith(field.label))} />
        </label>)}
      </div>
      <p className="wizard-caption">Acceptance thresholds are evaluated after collection. Leave a threshold blank to omit it. Minimum throughput is an acceptance goal, not request-rate pacing.</p>
      <div className="wizard-workload-grid">
        {SLA_FIELDS.map(field => <label className="wizard-field" key={field.key} htmlFor={`workload-${field.key}`}>
          <span className="wizard-label">{field.label}</span>
          <input className="field-input" disabled={nativeManaged} id={`workload-${field.key}`} type="number" min={0} max={field.max} step="any"
            value={inputNumber(load.slas[field.key], field.scale)} onChange={event => change(field.key, event.target.value, true, field.scale)}
            aria-invalid={issues.some(issue => issue.startsWith(field.label))} />
        </label>)}
      </div>
    </fieldset>
    {nativeManaged && <p className="wizard-caption">The reservation adds 15 minutes for initialization and collation, with a 30-minute minimum.</p>}
    <button type="button" className="btn btn-secondary btn-sm" onClick={resetWorkload}>Reset workload defaults</button>
    <p className="wizard-caption">Reset restores the displayed defaults and removes unsupported workload fields. Mapped release identity is retained.</p>
    {extraSlas.length > 0 && <p className="wizard-caption">Unsupported preset thresholds (reset workload defaults to remove): {extraSlas.map(([key, value]) => `${key}: ${String(value)}`).join(' · ')}</p>}
    {issues.length > 0 && <ul className="wizard-hint-list" role="alert">{issues.map(issue => <li key={issue}>{issue}</li>)}</ul>}
    <p className="wizard-caption">This wizard does not configure think time, arrival-rate pacing, custom ramp profiles or metric-triggered hard stops. Configure supported behavior in the provider script; the run’s Abort action remains available.</p>
    {draft.config.engine === 'sim' && <p className="wizard-caption">Simulation produces synthetic KPIs. Its connection may shorten the run duration; it does not send traffic to your environment.</p>}
  </section>
}

export function WorkloadPreview({ draft }: Pick<StepProps, 'draft'>) {
  const load = effectiveWorkload(draft.config.golden_configurable?.load_test)
  if (draft.config.engine === 'loadrunner') {
    const minutes = typeof load.duration_s === 'number' && typeof load.ramp_s === 'number'
      ? Math.max(30, Math.ceil((load.duration_s + load.ramp_s) / 60) + 15) : null
    return <div><p className="wizard-caption">Workload and acceptance are managed by the configured LoadRunner test. Verify its VUsers, pacing and SLAs before launch.</p><p>Reservation estimate: {String(load.duration_s)} seconds duration + {String(load.ramp_s)} seconds ramp, plus collation headroom. Timeslot: {minutes === null ? 'Invalid inputs' : `${minutes} minutes`}.</p></div>
  }
  return <dl className="wizard-review-dl">
    {WORKLOAD_FIELDS.map(field => <div className="wizard-review-pair" key={field.key}><dt>{field.label}</dt><dd>{String(load[field.key])}</dd></div>)}
    {SLA_FIELDS.map(field => <div className="wizard-review-pair" key={field.key}><dt>{field.label}</dt><dd>{load.slas[field.key] === undefined ? 'Not set' : String(inputNumber(load.slas[field.key], field.scale))}</dd></div>)}
    {Object.entries(load.slas).filter(([key]) => !SLA_FIELDS.some(field => field.key === key)).map(([key, value]) => <div className="wizard-review-pair" key={key}><dt>{key} (preset threshold)</dt><dd>{String(value)}</dd></div>)}
  </dl>
}
