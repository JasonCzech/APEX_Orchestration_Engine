type Row = Record<string, unknown>
const object = (value: unknown): value is Row =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value)
function snapshot(value: unknown): Row[] | null {
  if (
    !object(value) ||
    !Array.isArray(value.results) ||
    value.results.length > 20
  )
    return null
  const rows: Row[] = []
  const ids = new Set<string>()
  for (const row of value.results) {
    if (
      !object(row) ||
      typeof row.id !== 'string' ||
      ids.has(row.id) ||
      typeof row.engine !== 'string' ||
      typeof row.attempt !== 'number' ||
      typeof row.execution_status !== 'string' ||
      !Array.isArray(row.metrics) ||
      row.metrics.some((m) => !object(m) || typeof m.key !== 'string')
    )
      return null
    ids.add(row.id)
    rows.push(row)
  }
  return rows
}
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (object(value))
    return `{${Object.keys(value)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${canonical(value[k])}`)
      .join(',')}}`
  return JSON.stringify(value) ?? 'null'
}
const label = (row: Row) =>
  `${String(row.engine ?? 'Unknown engine')} · attempt ${String(row.attempt ?? 'Not available')} · ${String(row.id)}`
const outcome = (value: unknown) =>
  value === 'passed'
    ? 'Passed'
    : value === 'failed'
      ? 'Failed'
      : 'Not available'
const measured = (value: unknown) =>
  typeof value === 'number' && Number.isFinite(value)
    ? String(value)
    : 'Not available'
function metrics(row: Row): Map<string, Row> {
  return new Map(
    (Array.isArray(row.metrics) ? row.metrics : [])
      .filter((m): m is Row => object(m) && typeof m.key === 'string')
      .map((m) => [String(m.key), m]),
  )
}
const evidenceKeys = [
  'engine',
  'attempt',
  'execution_status',
  'test_outcome',
  'evidence_status',
  'metrics',
  'sla_breaches',
  'warnings',
  'bindings_verified',
  'started_at',
  'ended_at',
]
const content = (row: Row) =>
  Object.fromEntries(
    evidenceKeys.map((k) => [
      k,
      k === 'metrics' ? Object.fromEntries(metrics(row)) : row[k],
    ]),
  )

export function FrozenEvidenceDiff({
  selected,
  baseline,
}: {
  selected: unknown
  baseline: unknown
}) {
  const current = snapshot(selected)
  const previous = snapshot(baseline)
  const before = new Map(previous?.map((r) => [String(r.id), r]))
  const after = new Map(current?.map((r) => [String(r.id), r]))
  const added = previous
    ? (current?.filter((r) => !before.has(String(r.id))) ?? [])
    : []
  const removed = current
    ? (previous?.filter((r) => !after.has(String(r.id))) ?? [])
    : []
  const changed =
    current?.filter(
      (r) =>
        before.has(String(r.id)) &&
        canonical(content(r)) !== canonical(content(before.get(String(r.id))!)),
    ) ?? []
  return (
    <section className="cycle-panel" aria-label="Frozen evidence changes">
      <h3>Frozen evidence changes</h3>
      <p>
        Comparison version → selected version. Capture metadata alone does not
        mean measurements changed.
      </p>
      {!previous && (
        <p>
          The comparison version has no readable frozen evidence; its
          measurements cannot be verified here.
        </p>
      )}
      {!current && (
        <p>
          The selected version has no readable frozen evidence; its measurements
          cannot be verified here.
        </p>
      )}
      {current && previous && (
        <p>
          {added.length} attempts added · {removed.length} removed ·{' '}
          {changed.length} changed
        </p>
      )}
      {current &&
        previous &&
        !added.length &&
        !removed.length &&
        !changed.length && <p>Frozen execution evidence is unchanged.</p>}
      {added.length > 0 && (
        <>
          <h4>Added attempts</h4>
          <ul>
            {added.map((r) => (
              <li key={String(r.id)}>
                {label(r)} · {outcome(r.test_outcome)}
              </li>
            ))}
          </ul>
        </>
      )}
      {removed.length > 0 && (
        <>
          <h4>Removed attempts</h4>
          <ul>
            {removed.map((r) => (
              <li key={String(r.id)}>
                {label(r)} · {outcome(r.test_outcome)}
              </li>
            ))}
          </ul>
        </>
      )}
      {changed.map((r) => {
        const old = before.get(String(r.id))!
        const priorMetrics = metrics(old),
          nextMetrics = metrics(r)
        const keys = [
          ...new Set([...priorMetrics.keys(), ...nextMetrics.keys()]),
        ]
        return (
          <section key={String(r.id)} className="cycle-panel">
            <h4>{label(r)}</h4>
            <p>
              Test outcome: {outcome(old.test_outcome)} →{' '}
              {outcome(r.test_outcome)}
            </p>
            <p>
              Execution status: {String(old.execution_status)} →{' '}
              {String(r.execution_status)}
            </p>
            {['started_at', 'ended_at'].map(
              (key) =>
                old[key] !== r[key] && (
                  <p key={key}>
                    {key === 'started_at' ? 'Started' : 'Ended'}:{' '}
                    {String(old[key] ?? 'Not available')} →{' '}
                    {String(r[key] ?? 'Not available')}
                  </p>
                ),
            )}
            <dl>
              {keys
                .filter(
                  (k) =>
                    canonical(priorMetrics.get(k)) !==
                    canonical(nextMetrics.get(k)),
                )
                .map((k) => {
                  const prior = priorMetrics.get(k),
                    next = nextMetrics.get(k)
                  return (
                    <div key={k}>
                      <dt>{String(next?.label ?? prior?.label ?? k)}</dt>
                      <dd>
                        {measured(prior?.value)} {String(prior?.unit ?? '')} →{' '}
                        {measured(next?.value)} {String(next?.unit ?? '')}
                      </dd>
                    </div>
                  )
                })}
            </dl>
            {['sla_breaches', 'warnings'].map(
              (key) =>
                canonical(old[key]) !== canonical(r[key]) && (
                  <div key={key}>
                    <h5>
                      {key === 'sla_breaches'
                        ? 'SLA breaches'
                        : 'Evidence qualifications'}{' '}
                      changed
                    </h5>
                    <p>
                      Comparison:{' '}
                      {Array.isArray(old[key])
                        ? old[key].map(String).join('; ') || 'None recorded'
                        : 'Not available'}
                    </p>
                    <p>
                      Selected:{' '}
                      {Array.isArray(r[key])
                        ? r[key].map(String).join('; ') || 'None recorded'
                        : 'Not available'}
                    </p>
                  </div>
                ),
            )}
            {(old.evidence_status !== r.evidence_status ||
              old.bindings_verified !== r.bindings_verified) && (
              <p>
                Evidence availability or binding verification changed. Inspect
                both snapshots before review.
              </p>
            )}
          </section>
        )
      })}
    </section>
  )
}
