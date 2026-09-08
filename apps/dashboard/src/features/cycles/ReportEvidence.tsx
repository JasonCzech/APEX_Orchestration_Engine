import { useRef } from 'react'
import { Link } from 'react-router'
import {
  useCycleResults,
  useFreezeReportEvidence,
  type Cycle,
  type Version,
} from './api'

export function ReportEvidence({
  cycle,
  version,
  canEdit,
}: {
  cycle: Cycle
  version: Version
  canEdit: boolean
}) {
  const results = useCycleResults(cycle.id, '')
  const capture = useFreezeReportEvidence(cycle.id, version.id)
  const operation = useRef<{ signature: string; key: string } | null>(null)
  const frozen = version.content._execution_evidence
  const snapshot =
    frozen && typeof frozen === 'object' && !Array.isArray(frozen)
      ? (frozen as Record<string, unknown>)
      : null
  const rows =
    snapshot && Array.isArray(snapshot.results)
      ? (snapshot.results as Record<string, unknown>[])
      : []
  const candidates = results.data?.results ?? []
  const active =
    !version.archived && !cycle.versions.some((v) => v.replaces === version.id)
  return (
    <section
      className="cycle-panel glass-panel"
      aria-label="Frozen report evidence"
    >
      <h3>Frozen report evidence</h3>
      {snapshot ? (
        <>
          <p>
            Captured {String(snapshot.captured_at)} by{' '}
            {String(snapshot.captured_by)}. This snapshot is part of this report
            version's checksum and review.
          </p>
          <ul>
            {rows.map((r) => (
              <li key={String(r.id)}>
                {String(r.engine)} · attempt {String(r.attempt)} · execution{' '}
                {String(r.execution_status)} · test outcome{' '}
                {r.test_outcome == null
                  ? 'Not available'
                  : String(r.test_outcome)}
                <dl>
                  {Array.isArray(r.metrics) &&
                    r.metrics.map((m: Record<string, unknown>) => (
                      <div key={String(m.key)}>
                        <dt>
                          {String(m.label)} ({String(m.unit)})
                        </dt>
                        <dd>
                          {m.value == null ? 'Not available' : String(m.value)}
                        </dd>
                      </div>
                    ))}
                </dl>
                {Array.isArray(r.sla_breaches) && r.sla_breaches.length > 0 && (
                  <p>SLA breaches: {r.sla_breaches.map(String).join('; ')}</p>
                )}
                <p>
                  Bindings remain unverified. This captures history summaries,
                  not raw provider artifacts.
                </p>
              </li>
            ))}
          </ul>
          <details>
            <summary>Snapshot technical details and qualifications</summary>
            <pre className="cycle-technical">
              {JSON.stringify(snapshot, null, 2)}
            </pre>
          </details>
        </>
      ) : (
        <p>
          This report has no frozen execution summaries. Written evidence
          references have not been verified.
        </p>
      )}
      {canEdit && active && (
        <>
          <p>
            Capture the linked summaries shown above as a new draft. This
            preserves unknown outcomes and evidence qualifications; it does not
            verify raw artifacts, publish a report or carry forward approval.
          </p>
          <p>
            {candidates.length} linked attempts will be captured. Refresh
            results above before capture if needed.
          </p>
          <button
            className="btn btn-primary"
            disabled={
              capture.isPending ||
              results.isFetching ||
              Boolean(results.error) ||
              !candidates.length ||
              candidates.length > 20
            }
            onClick={() => {
              const expected_results = Object.fromEntries(
                candidates.map((r) => [r.id, r.evidence_checksum]),
              )
              const signature = JSON.stringify({
                version: version.id,
                expected_results,
              })
              if (operation.current?.signature !== signature)
                operation.current = {
                  signature,
                  key: crypto.randomUUID().replaceAll('-', ''),
                }
              capture.mutate({
                expected_revision: cycle.revision,
                idempotency_key: operation.current.key,
                expected_results,
              })
            }}
          >
            {capture.isPending
              ? 'Capturing…'
              : 'Capture evidence as new report draft'}
          </button>
          {candidates.length > 20 && (
            <p>
              Capture supports up to 20 selected attempts through the API or
              CLI.
            </p>
          )}
        </>
      )}
      {capture.error && <p role="alert">{capture.error.message}</p>}
      {capture.data && (
        <p role="status">
          Evidence captured; renewed review is required.{' '}
          <Link
            to={`?tab=report&version=${encodeURIComponent(capture.data.versions.find((v) => v.replaces === version.id)?.id ?? version.id)}`}
          >
            Open captured draft
          </Link>
        </p>
      )}
    </section>
  )
}
