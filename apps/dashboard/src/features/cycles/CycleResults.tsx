import { useState } from 'react'
import { Link } from 'react-router'
import { useCycleResults } from './api'

const number = (value: number) =>
  new Intl.NumberFormat(undefined, { maximumSignificantDigits: 6 }).format(
    value,
  )
export function CycleResults({ cycleId }: { cycleId: string }) {
  const [baseline, setBaseline] = useState('')
  const query = useCycleResults(cycleId, baseline)
  return (
    <section
      className="cycle-panel glass-panel"
      aria-label="Linked execution results"
    >
      <h3>Linked execution results</h3>
      <p>
        Each engine and attempt keeps its own result. These history summaries
        can lag execution evidence. Linking a run does not verify its script,
        workload or environment against this cycle.
      </p>
      <div className="cycle-actions">
        <button
          className="btn btn-secondary"
          onClick={() => void query.refetch()}
          disabled={query.isFetching}
        >
          Refresh results
        </button>
        <Link to="?tab=executions">Link an existing attempt</Link>
      </div>
      {query.isPending && <p role="status">Loading linked results…</p>}
      {query.error && (
        <p role="alert">
          {query.error.message}{' '}
          <button className="btn btn-secondary" onClick={() => setBaseline('')}>
            Clear comparison
          </button>
        </p>
      )}
      {query.data && (
        <>
          <p>
            History read at {new Date(query.data.read_at).toLocaleString()}.
            This is the retrieval time, not provider update freshness.
          </p>
          {query.data.results.length === 0 ? (
            <p>No linked execution evidence yet.</p>
          ) : (
            <>
              <label className="cycle-field">
                Comparison baseline
                <select
                  value={baseline}
                  onChange={(event) => setBaseline(event.target.value)}
                >
                  <option value="">No comparison</option>
                  {query.data.results.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.engine} · attempt {r.attempt} · {r.thread_id}
                    </option>
                  ))}
                </select>
              </label>
              <p>
                Comparison does not promote a baseline. Percentage differences
                are unavailable when the baseline is zero or either value is
                missing. Differences alone do not establish improvement.
              </p>
              {query.data.results.map((r) => (
                <article
                  key={r.id}
                  className="cycle-panel"
                  aria-label={`${r.engine} attempt ${r.attempt} ${r.id}`}
                >
                  <h4>
                    {r.engine} · attempt {r.attempt}
                    {baseline === r.id ? ' · Selected baseline' : ''}
                  </h4>
                  <p>
                    Execution status: <strong>{r.execution_status}</strong> ·
                    Test outcome:{' '}
                    <strong>
                      {r.test_outcome === 'passed'
                        ? 'Passed'
                        : r.test_outcome === 'failed'
                          ? 'Failed'
                          : 'Not available'}
                    </strong>
                  </p>
                  <p>
                    Summary evidence: {r.evidence_status} · Started{' '}
                    {new Date(r.started_at).toLocaleString()}
                    {r.ended_at
                      ? ` · Ended ${new Date(r.ended_at).toLocaleString()}`
                      : ''}
                  </p>
                  <dl>
                    {r.metrics.map((m) => (
                      <div key={m.key} className="cycle-panel">
                        <dt>
                          {m.label} ({m.unit})
                        </dt>
                        <dd>
                          {m.value == null ? 'Not available' : number(m.value)}{' '}
                          ·{' '}
                          {m.preference === 'context'
                            ? 'Interpret in workload context'
                            : `${m.preference === 'higher' ? 'Higher' : 'Lower'} is generally preferable at equivalent workload`}
                          {baseline && baseline !== r.id && (
                            <p>
                              {m.difference == null
                                ? 'Difference not available'
                                : `${m.difference === 0 ? 'Equal to baseline' : `${number(Math.abs(m.difference))} ${m.unit} ${m.difference > 0 ? 'higher' : 'lower'} than baseline`} · ${m.percent_difference == null ? 'Percentage not available' : `${number(Math.abs(m.percent_difference))}% ${m.percent_difference === 0 ? 'change' : m.percent_difference > 0 ? 'higher' : 'lower'}`}`}
                            </p>
                          )}
                        </dd>
                      </div>
                    ))}
                  </dl>
                  {r.sla_breaches.length > 0 && (
                    <>
                      <h5>SLA breaches</h5>
                      <ul>
                        {r.sla_breaches.map((b, i) => (
                          <li key={i}>{b}</li>
                        ))}
                      </ul>
                    </>
                  )}
                  <details>
                    <summary>Evidence qualifications</summary>
                    <ul>
                      {r.warnings.map((w) => (
                        <li key={w}>{w}</li>
                      ))}
                    </ul>
                  </details>
                  <Link
                    to={`/runs/${encodeURIComponent(r.thread_id)}/phases/execution`}
                  >
                    Inspect execution evidence
                  </Link>
                </article>
              ))}
            </>
          )}
        </>
      )}
    </section>
  )
}
