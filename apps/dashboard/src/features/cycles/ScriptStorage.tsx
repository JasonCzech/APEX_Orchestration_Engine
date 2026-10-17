import { formatTimestamp } from '@/utils/time'
import { useState } from 'react'
import {
  downloadScript,
  useScriptLibrary,
  useStoreScript,
  type Cycle,
  type Version,
} from './api'

export function ScriptStorage({
  cycle,
  version,
  canEdit,
}: {
  cycle: Cycle
  version: Version
  canEdit: boolean
}) {
  const query = useScriptLibrary(cycle.id)
  const store = useStoreScript(cycle.id, version.id)
  const [connection, setConnection] = useState('')
  const [downloadError, setDownloadError] = useState('')
  const [downloading, setDownloading] = useState(false)
  const artifact = query.data?.artifacts.find(
    (a) => a.version_id === version.id,
  )
  const supported = ['apex_native_json', 'web_http_html'].includes(
    String(version.content.format),
  )
  return (
    <section
      className="cycle-panel glass-panel"
      aria-label="Script source storage"
    >
      <h3>Immutable script source</h3>
      <p>
        {version.title} · version <code>{version.id}</code>
      </p>
      <p>
        Store the approved source bytes in APEX and verify their SHA-256
        checksum. This does not validate the script with an execution provider
        or launch a test.
      </p>
      {query.isPending && <p role="status">Loading script storage…</p>}
      {query.error && (
        <p role="alert">
          {query.error.message}{' '}
          <button onClick={() => void query.refetch()}>Retry</button>
        </p>
      )}
      {artifact && (
        <>
          <p>
            <strong>
              {artifact.stored_at
                ? 'Stored and checksum verified'
                : 'Storage pending — retry required'}
            </strong>
          </p>
          <p>
            {artifact.size_bytes.toLocaleString()} bytes · source SHA-256{' '}
            <code>{artifact.source_checksum}</code>
          </p>
          <p>
            Storage:{' '}
            {query.data?.connections.find(
              (c) => c.id === artifact.connection_id,
            )?.name ?? artifact.connection_id}
            .{' '}
            {artifact.stored_at &&
              `Verified ${formatTimestamp(artifact.stored_at)}.`}
          </p>
          <details>
            <summary>Storage identity</summary>
            <p>
              Record: <code>{artifact.id}</code>. Connection generation:{' '}
              {artifact.connection_version}. Approved content checksum:{' '}
              <code>{artifact.version_checksum}</code>.
            </p>
          </details>
          {artifact.stored_at && (
            <button
              className="btn btn-secondary"
              disabled={downloading}
              onClick={async () => {
                setDownloading(true)
                setDownloadError('')
                try {
                  const data = await downloadScript(cycle.id, version.id)
                  const url = URL.createObjectURL(data)
                  const anchor = document.createElement('a')
                  anchor.href = url
                  anchor.download =
                    version.content.format === 'apex_native_json'
                      ? 'script.json'
                      : 'script.c'
                  anchor.click()
                  setTimeout(() => URL.revokeObjectURL(url), 1000)
                } catch (error) {
                  setDownloadError(
                    error instanceof Error ? error.message : 'Download failed',
                  )
                } finally {
                  setDownloading(false)
                }
              }}
            >
              {downloading ? 'Verifying download…' : 'Download verified source'}
            </button>
          )}
        </>
      )}
      {downloadError && <p role="alert">{downloadError}</p>}
      {!version.approval_current && (
        <p>
          Approve this exact script version and its upstream dependencies before
          storing source.
        </p>
      )}
      {!supported && (
        <p>
          External import references and binary bundles must be materialized
          through a provider import workflow. This source snapshot supports
          native JSON and Web HTTP/HTML text.
        </p>
      )}
      {canEdit && version.approval_current && supported && query.data && (
        <form
          onSubmit={(event) => {
            event.preventDefault()
            store.mutate({
              expected_revision: query.data.revision,
              connection_id: artifact?.connection_id ?? connection,
            })
          }}
        >
          {!artifact && (
            <label className="cycle-field">
              Persistent artifact store
              <select
                required
                value={connection}
                disabled={store.isPending}
                onChange={(event) => setConnection(event.target.value)}
              >
                <option value="">Select an S3-compatible store</option>
                {query.data.connections.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          {!artifact && !query.data.connections.length && (
            <p>
              Ask an administrator to configure an enabled S3-compatible
              artifact connection for this project.
            </p>
          )}
          <button
            className="btn btn-secondary"
            disabled={store.isPending || (!artifact && !connection)}
          >
            {store.isPending
              ? 'Verifying storage…'
              : artifact?.stored_at
                ? 'Recheck stored source'
                : artifact
                  ? 'Retry source storage'
                  : 'Store approved source'}
          </button>
        </form>
      )}
      {store.error && <p role="alert">{store.error.message}</p>}
      {store.isSuccess && (
        <p role="status">
          Approved source checksum verified. Prepare a new execution to include
          this storage binding.
        </p>
      )}
    </section>
  )
}
