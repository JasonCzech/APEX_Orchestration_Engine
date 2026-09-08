import { useRef } from 'react'
import { useScriptValidations, useValidateScript } from './api'

const labels: Record<string, string> = {
  accepted: 'Source accepted by provider',
  rejected: 'Source rejected by provider',
  unavailable: 'Validation unavailable',
  pending: 'Validation pending — resume to recover',
}
export function ProviderValidation({
  cycleId,
  preparationId,
  canEdit,
}: {
  cycleId: string
  preparationId: string
  canEdit: boolean
}) {
  const query = useScriptValidations(cycleId, preparationId)
  const validate = useValidateScript(cycleId, preparationId)
  const requestKey = useRef<string | null>(null)
  const latest = query.data?.validations[0]
  const pending = latest?.status === 'pending' ? latest : undefined
  return (
    <section aria-label="Provider source validation">
      <h4>Provider source validation</h4>
      <p>
        Validates the stored native script with APEX Load. Tests are not run.
        Workload, capacity, target readiness and launch authorization require
        separate checks.
      </p>
      {query.isPending && <p role="status">Loading validation history…</p>}
      {query.error && (
        <p role="alert">
          {query.error.message}{' '}
          <button onClick={() => void query.refetch()}>Retry history</button>
        </p>
      )}
      {query.data && (
        <>
          {query.data.blockers.length > 0 && (
            <ul>
              {query.data.blockers.map((b) => (
                <li key={b}>{b}</li>
              ))}
            </ul>
          )}
          {canEdit && (
            <button
              className="btn btn-secondary"
              disabled={validate.isPending || query.data.blockers.length > 0}
              onClick={() => {
                if (!query.data) return
                requestKey.current ??=
                  pending?.request_key ??
                  crypto.randomUUID().replaceAll('-', '')
                validate.mutate(
                  {
                    expected_revision: query.data.revision,
                    idempotency_key: requestKey.current,
                  },
                  {
                    onSuccess: () => {
                      requestKey.current = null
                    },
                  },
                )
              }}
            >
              {validate.isPending
                ? 'Validating source…'
                : pending
                  ? 'Resume provider validation'
                  : 'Validate with APEX Load'}
            </button>
          )}
          {query.data.validations.length === 0 && (
            <p>No provider validation recorded.</p>
          )}
          <ul>
            {query.data.validations.map((v) => (
              <li key={v.id}>
                <strong>
                  {labels[v.status] ?? 'Validation status unknown'}
                </strong>
                {v.status === 'accepted' && (
                  <p>
                    {v.current
                      ? 'Current source acceptance (valid for up to 24 hours while bindings remain current).'
                      : 'Historical acceptance — refresh prerequisites and validate again.'}
                  </p>
                )}
                <p>
                  {v.finished_at
                    ? `Finished ${new Date(v.finished_at).toLocaleString()}`
                    : `Started ${new Date(v.created_at).toLocaleString()}`}{' '}
                  · {v.created_by}
                </p>
                {v.issues.map((issue, i) => (
                  <p key={`issue-${i}`}>{issue}</p>
                ))}
                {v.warnings.map((warning, i) => (
                  <p key={`warning-${i}`}>Warning: {warning}</p>
                ))}
                <details>
                  <summary>Operation identity</summary>
                  <code>{v.id}</code>
                </details>
              </li>
            ))}
          </ul>
        </>
      )}
      {validate.error && <p role="alert">{validate.error.message}</p>}
      {validate.isSuccess && (
        <p role="status">
          {labels[validate.data.status] ?? 'Validation recorded'}. No test was
          launched.
        </p>
      )}
    </section>
  )
}
