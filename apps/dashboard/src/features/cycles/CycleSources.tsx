import { useRef, useState } from 'react'
import { Link } from 'react-router'
import {
  useCycleSources,
  useCaptureCycleSource,
  type Cycle,
  type Version,
} from './api'

export function CycleSources({
  cycle,
  version,
  canEdit,
}: {
  cycle: Cycle
  version: Version
  canEdit: boolean
}) {
  const [open, setOpen] = useState(false),
    [connection, setConnection] = useState(''),
    [search, setSearch] = useState(''),
    [query, setQuery] = useState(''),
    [page, setPage] = useState(''),
    [revision, setRevision] = useState('')
  const catalog = useCycleSources(cycle.id, connection, query, '', '', open)
  const preview = useCycleSources(
    cycle.id,
    connection,
    '',
    page,
    revision,
    open && Boolean(page),
  )
  const capture = useCaptureCycleSource(cycle.id, version.id)
  const operation = useRef<{ signature: string; key: string } | null>(null)
  const records = Array.isArray(version.content._source_evidence)
    ? (version.content._source_evidence as Record<string, unknown>[])
    : []
  const active =
    !version.archived && !cycle.versions.some((v) => v.replaces === version.id)
  return (
    <section
      className="cycle-panel glass-panel"
      aria-label="Connected scope sources"
    >
      <h3>Captured source revisions</h3>
      {!records.length && (
        <p>No connected source revisions captured in this scope.</p>
      )}
      {records.map((r, i) => {
        const document = r.document as Record<string, unknown>,
          ref = document.ref as Record<string, unknown>
        return (
          <article key={i} className="cycle-panel">
            <h4>{String(r.title ?? r.label)}</h4>
            <p>
              Confluence · page {String(ref.id)} · revision{' '}
              {String(ref.revision)} · captured {String(r.captured_at)}
            </p>
            <p>
              Included in this scope version. Revalidate freshness for the next
              cycle.
            </p>
            <details>
              <summary>Captured page text</summary>
              <pre className="cycle-technical">{String(document.text)}</pre>
            </details>
          </article>
        )
      })}
      <details onToggle={(e) => setOpen(e.currentTarget.open)}>
        <summary>Find Confluence Cloud pages</summary>
        <p>
          Browse up to 20 pages in the configured space, or search by exact page
          title. Capture creates a new scope draft and requires renewed review.
        </p>
        {catalog.error && (
          <p role="alert">
            {catalog.error.message}{' '}
            <button
              className="btn btn-secondary"
              onClick={() => void catalog.refetch()}
            >
              Retry sources
            </button>
          </p>
        )}
        {catalog.isFetching && <p role="status">Reading source catalog…</p>}
        {catalog.data && (
          <>
            {catalog.data.blockers.map((b) => (
              <p key={b}>{b}</p>
            ))}
            <fieldset
              disabled={capture.isPending}
              className="cycle-execution-fields"
            >
              <legend>Source selection</legend>
              <label className="cycle-field">
                Confluence connection
                <select
                  value={connection}
                  onChange={(e) => {
                    setConnection(e.target.value)
                    setPage('')
                    setRevision('')
                    capture.reset()
                  }}
                >
                  <option value="">Select connection</option>
                  {catalog.data.connections.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
              </label>
              {connection && (
                <>
                  <form
                    onSubmit={(e) => {
                      e.preventDefault()
                      setQuery(search)
                      setPage('')
                      setRevision('')
                      capture.reset()
                    }}
                  >
                    <label className="cycle-field">
                      Exact page title
                      <input
                        maxLength={500}
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                      />
                    </label>
                    <button className="btn btn-secondary" type="submit">
                      Search pages
                    </button>
                  </form>
                  <label className="cycle-field">
                    Confluence page
                    <select
                      value={page}
                      onChange={(e) => {
                        setPage(e.target.value)
                        setRevision(
                          catalog.data.hits.find(
                            (h) => h.ref.id === e.target.value,
                          )?.ref.revision ?? '',
                        )
                        capture.reset()
                      }}
                    >
                      <option value="">Select a page</option>
                      {catalog.data.hits.map((h) => (
                        <option key={h.ref.id} value={h.ref.id}>
                          {h.title} · revision {h.ref.revision}
                        </option>
                      ))}
                    </select>
                  </label>
                  {!catalog.data.hits.length && !catalog.isFetching && (
                    <p>
                      No matching pages returned. Check the exact title and
                      space access.
                    </p>
                  )}
                </>
              )}
            </fieldset>
          </>
        )}
        {preview.error && (
          <p role="alert">
            {preview.error.message}{' '}
            <button
              className="btn btn-secondary"
              onClick={() => void preview.refetch()}
            >
              Refresh page
            </button>
          </p>
        )}
        {preview.data?.document && (
          <section aria-label="Page revision preview">
            <h4>{preview.data.document.title}</h4>
            <p>
              Page {preview.data.document.ref.id} · revision{' '}
              {preview.data.document.ref.revision}
            </p>
            <pre className="cycle-technical">{preview.data.document.text}</pre>
            {canEdit && active && (
              <button
                className="btn btn-primary"
                disabled={capture.isPending || preview.isFetching}
                onClick={() => {
                  const data = preview.data
                  if (
                    !data.document ||
                    !data.connection_version ||
                    !data.checksum
                  )
                    return
                  const logical = {
                    connection_id: connection,
                    connection_version: data.connection_version,
                    ref: data.document.ref,
                    expected_checksum: data.checksum,
                    title: data.document.title ?? 'Confluence page',
                  }
                  const signature = JSON.stringify(logical)
                  if (operation.current?.signature !== signature)
                    operation.current = {
                      signature,
                      key: crypto.randomUUID().replaceAll('-', ''),
                    }
                  capture.mutate({
                    ...logical,
                    expected_revision: data.revision,
                    idempotency_key: operation.current.key,
                  })
                }}
              >
                {capture.isPending
                  ? 'Capturing…'
                  : 'Capture page as new scope draft'}
              </button>
            )}
          </section>
        )}
        {capture.error && <p role="alert">{capture.error.message}</p>}
        {capture.data && (
          <p role="status">
            Source revision captured.{' '}
            <Link
              to={`?tab=scope&version=${encodeURIComponent(capture.data.versions.find((v) => v.replaces === version.id)?.id ?? version.id)}`}
            >
              Open captured scope draft
            </Link>
          </p>
        )}
      </details>
    </section>
  )
}
