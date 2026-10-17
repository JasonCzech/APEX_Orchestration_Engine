import { formatTimestamp } from '@/utils/time'
import { useRef, useState } from 'react'
import { Link, useBeforeUnload } from 'react-router'
import { Dialog } from '@/components/Dialog'
import { useNavigationBlocker } from '@/components/useNavigationBlocker'
import {
  useProviderScripts,
  useProviderImport,
  type Cycle,
  type ProviderImport,
} from './api'

export function ProviderScriptImport({
  cycle,
  canEdit,
}: {
  cycle: Cycle
  canEdit: boolean
}) {
  const [open, setOpen] = useState(false)
  const [connection, setConnection] = useState('')
  const [remoteId, setRemoteId] = useState('')
  const [offset, setOffset] = useState(0)
  const [plan, setPlan] = useState('')
  const [title, setTitle] = useState('')
  const [parameters, setParameters] = useState('')
  const [coverage, setCoverage] = useState('')
  const catalog = useProviderScripts(cycle.id, connection, '', offset, open)
  const preview = useProviderScripts(
    cycle.id,
    connection,
    remoteId,
    0,
    open && Boolean(remoteId),
  )
  const importer = useProviderImport(cycle.id)
  const key = useRef<{ signature: string; value: string } | null>(null)
  const dirty = Boolean(parameters || coverage || title || plan) && !importer.isSuccess
  const blocker = useNavigationBlocker(dirty && !importer.isPending)
  useBeforeUnload((event) => {
    if (dirty) event.preventDefault()
  })
  const exported = preview.data?.preview
  return (
    <>
      <details
        className="cycle-panel glass-panel"
        onToggle={(event) => setOpen(event.currentTarget.open)}
      >
        <summary>Import from the APEX Load library</summary>
        <p>
          Capture a provider export as a new draft. Exports may differ from
          executable provider content. Review, storage and validation remain
          required; this does not bind or launch a test.
        </p>
        {catalog.isFetching && <p role="status">Reading provider library…</p>}
        {catalog.error && (
          <p role="alert">
            {catalog.error.message}{' '}
            <button onClick={() => void catalog.refetch()}>
              Retry library
            </button>
          </p>
        )}
        {catalog.data && (
          <>
            {catalog.data.blockers.map((b) => (
              <p key={b}>{b}</p>
            ))}
            {catalog.data.connections.length === 0 && <p className="tonal-card info">No APEX Load connection is available for this project. Ask an administrator to configure an enabled provider connection.</p>}
            <fieldset
              disabled={importer.isPending}
              className="cycle-execution-fields"
            >
              <legend>Choose source</legend>
              <label className="cycle-field">
                APEX Load connection
                <select
                  value={connection}
                  onChange={(e) => {
                    setConnection(e.target.value)
                    setRemoteId('')
                    setOffset(0)
                    importer.reset()
                  }}
                >
                  <option value="">Select a provider connection</option>
                  {catalog.data.connections.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
              </label>
              {connection && (
                <>
                  <label className="cycle-field">
                    Provider script
                    <select
                      value={remoteId}
                      onChange={(e) => {
                        setRemoteId(e.target.value)
                        setTitle(
                          catalog.data?.scripts.find(
                            (s) => s.id === e.target.value,
                          )?.name ?? '',
                        )
                        importer.reset()
                      }}
                    >
                      <option value="">Select an existing script</option>
                      {catalog.data.scripts.map((s) => (
                        <option key={s.id} value={s.id}>
                          {s.name} · {s.protocol} ·{' '}
                          {s.version || 'Version not supplied'}
                        </option>
                      ))}
                    </select>
                  </label>
                  <div className="cycle-actions">
                    <button
                      disabled={offset === 0 || catalog.isFetching}
                      onClick={() => {
                        setOffset(Math.max(0, offset - 20))
                        setRemoteId('')
                      }}
                    >
                      Previous scripts
                    </button>
                    <span>
                      {catalog.data.total} scripts · page{' '}
                      {Math.floor(offset / 20) + 1}
                    </span>
                    <button
                      disabled={
                        offset + 20 >= catalog.data.total || catalog.isFetching
                      }
                      onClick={() => {
                        setOffset(offset + 20)
                        setRemoteId('')
                      }}
                    >
                      Next scripts
                    </button>
                  </div>
                </>
              )}
            </fieldset>
            {preview.isFetching && remoteId && (
              <p role="status">Reading source preview…</p>
            )}
            {preview.error && (
              <p role="alert">
                {preview.error.message}{' '}
                <button onClick={() => void preview.refetch()}>
                  Refresh preview
                </button>
              </p>
            )}
            {exported && remoteId && (
              <section aria-label="Provider export preview">
                <h3>{exported.name}</h3>
                <p>
                  Provider project: {exported.project_id} · version{' '}
                  {exported.version || 'Not supplied'}. Executable content has
                  not been verified.
                </p>
                <p>
                  Export SHA-256: <code>{exported.source_checksum}</code>
                </p>
                <details>
                  <summary>Captured source</summary>
                  <pre className="cycle-technical">{exported.source}</pre>
                </details>
                {canEdit && (
                  <form
                    onSubmit={(event) => {
                      event.preventDefault()
                      if (!preview.data?.connection_version || !plan) return
                      const logical: Omit<
                        ProviderImport,
                        'expected_revision' | 'idempotency_key'
                      > = {
                        connection_id: connection,
                        connection_version: preview.data.connection_version,
                        remote_script_id: remoteId,
                        expected_source_checksum: exported.source_checksum,
                        plan_version_id: plan,
                        title,
                        parameters,
                        coverage,
                      }
                      const signature = JSON.stringify(logical)
                      if (key.current?.signature !== signature)
                        key.current = {
                          signature,
                          value: crypto.randomUUID().replaceAll('-', ''),
                        }
                      importer.mutate({
                        ...logical,
                        expected_revision: preview.data.revision,
                        idempotency_key: key.current.value,
                      })
                    }}
                  >
                    <fieldset
                      disabled={importer.isPending}
                      className="cycle-execution-fields"
                    >
                      <legend>Reviewable draft</legend>
                      <label className="cycle-field">
                        Approved test plan
                        <select
                          required
                          value={plan}
                          onChange={(e) => {
                            setPlan(e.target.value)
                            importer.reset()
                          }}
                        >
                          <option value="">Select a plan</option>
                          {cycle.versions
                            .filter(
                              (v) =>
                                v.kind === 'test_plan' && v.approval_current,
                            )
                            .map((v) => (
                              <option key={v.id} value={v.id}>
                                {v.title} · {v.id}
                              </option>
                            ))}
                        </select>
                      </label>
                      <label className="cycle-field">
                        Draft title
                        <input
                          required
                          maxLength={255}
                          value={title}
                          onChange={(e) => {
                            setTitle(e.target.value)
                            importer.reset()
                          }}
                        />
                      </label>
                      <label className="cycle-field">
                        Parameter files and dependencies
                        <textarea
                          required
                          maxLength={10000}
                          value={parameters}
                          onChange={(e) => {
                            setParameters(e.target.value)
                            importer.reset()
                          }}
                        />
                      </label>
                      <label className="cycle-field">
                        Requirement and journey coverage
                        <textarea
                          required
                          maxLength={10000}
                          value={coverage}
                          onChange={(e) => {
                            setCoverage(e.target.value)
                            importer.reset()
                          }}
                        />
                      </label>
                      <button
                        className="btn btn-primary"
                        disabled={preview.isFetching}
                        type="submit"
                      >
                        {importer.isPending
                          ? 'Importing…'
                          : 'Import export as draft'}
                      </button>
                    </fieldset>
                  </form>
                )}
              </section>
            )}
            {importer.error && (
              <p role="alert">
                {importer.error.message} Your draft details are retained.
                Refresh the preview if its source or connection changed.
              </p>
            )}
            {importer.isSuccess && (
              <p role="status">
                Provider export imported as a draft.{' '}
                <Link
                  to={`?tab=script&version=${encodeURIComponent(importer.data.version_id)}`}
                >
                  Review imported draft
                </Link>
              </p>
            )}
            {catalog.data.imports.length > 0 && (
              <section>
                <h3>Captured provider exports</h3>
                <ul>
                  {catalog.data.imports.map((i) => (
                    <li key={i.id}>
                      <Link
                        to={`?tab=script&version=${encodeURIComponent(i.version_id)}`}
                      >
                        {i.remote_name} · provider version{' '}
                        {i.remote_version || 'not supplied'}
                      </Link>
                      <p>
                        Captured {formatTimestamp(i.imported_at)} by{' '}
                        {i.imported_by}. Executable content unverified.
                      </p>
                      <details>
                        <summary>Source provenance</summary>
                        <p>
                          Provider project {i.remote_project_id} · connection{' '}
                          {i.connection_id} · generation {i.connection_version}
                        </p>
                        <code>{i.source_checksum}</code>
                      </details>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </>
        )}
      </details>
      {blocker.state === 'blocked' && (
        <Dialog
          ariaLabel="Unsaved provider import"
          className="cycle-leave-dialog"
          overlayClassName="cycle-dialog-overlay"
          onClose={() => blocker.reset()}
        >
          <h2>Leave unsaved import details?</h2>
          <p>The import title, plan and supporting details have not been saved.</p>
          <div className="ux-dialog-actions">
            <button className="btn btn-primary" onClick={() => blocker.reset()}>Keep editing</button>
            <button className="btn btn-secondary" onClick={() => blocker.proceed()}>Discard and leave</button>
          </div>
        </Dialog>
      )}
    </>
  )
}
