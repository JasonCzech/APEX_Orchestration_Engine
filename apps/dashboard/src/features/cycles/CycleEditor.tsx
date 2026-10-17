import { useState, type FormEvent } from 'react'
import { Link, useBeforeUnload } from 'react-router'
import { useNavigationBlocker } from '@/components/useNavigationBlocker'
import { Dialog } from '@/components/Dialog'
import { ApiError } from '@/api/errors'
import { useCycle } from './api'
import { useEnvironmentInventory } from '@/api/hooks/useInventory'
import {
  useVersionWrite,
  type Cycle,
  type Field,
  type Version,
  type VersionWrite,
} from './api'

function FieldInput({
  field,
  value,
  onChange,
}: {
  field: Field
  value: unknown
  onChange: (value: string | number) => void
}) {
  const id = `cycle-field-${field.key}`
  const helpId = `${id}-help`
  const shared = {
    id,
    'aria-required': field.required,
    'aria-describedby': field.help ? helpId : undefined,
    value: typeof value === 'string' || typeof value === 'number' ? value : '',
    onChange: (
      event: React.ChangeEvent<
        HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement
      >,
    ) =>
      onChange(
        field.control === 'number' && event.target.value !== ''
          ? Number(event.target.value)
          : event.target.value,
      ),
  }
  return (
    <div className="cycle-field">
      <label htmlFor={id}>
        {field.label}
        {field.required ? ' *' : ' (optional)'}
      </label>
      {field.control === 'select' ? (
        <select {...shared}>
          <option value="">Select…</option>
          {field.options?.map((option) => (
            <option key={option} value={option}>
              {({ 'apex-load': 'APEX Load', loadrunner: 'LoadRunner', apex_native_json: 'APEX native JSON', web_http_html: 'Web HTTP/HTML', other_import: 'Other import' } as Record<string, string>)[option] ?? option.replaceAll('_', ' ')}
            </option>
          ))}
        </select>
      ) : field.control === 'number' ? (
        <input
          {...shared}
          type="number"
          min={field.minimum ?? undefined}
          max={field.maximum ?? undefined}
          step={('integer' in field && field.integer) || ['vusers', 'target_version'].includes(field.key) ? 1 : 'any'}
        />
      ) : field.control === 'text' ? (
        <input {...shared} type="text" maxLength={50000} />
      ) : (
        <textarea
          {...shared}
          rows={field.key === 'source' ? 12 : 4}
          maxLength={50000}
          spellCheck={field.key !== 'source'}
        />
      )}
      {field.help && <small id={helpId}>{field.help}</small>}
    </div>
  )
}

export function CycleEditor({
  cycle,
  kind,
  base,
  onSaved,
  onCancel,
}: {
  cycle: Cycle
  kind: VersionWrite['kind']
  base?: Version
  onSaved: (id: string) => void
  onCancel: () => void
}) {
  const [title, setTitle] = useState(base?.title ?? '')
  const [content, setContent] = useState<Record<string, unknown>>({
    ...base?.content,
  })
  const [dependencies, setDependencies] = useState<string[]>(
    base?.dependencies ?? [],
  )
  const [fileError, setFileError] = useState('')
  const [fileLoading, setFileLoading] = useState(false)
  const [fileName, setFileName] = useState('')
  const [change, setChange] = useState('')
  const [cancelRequested, setCancelRequested] = useState(false)
  const freshCycle = useCycle(cycle.id, false)
  const inventory = useEnvironmentInventory(kind === 'readiness' ? cycle.environment_id : undefined)
  // Pin the revision at editor creation: background reads must never silently rebase a draft.
  const [revision, setRevision] = useState(cycle.revision)
  const [replaces, setReplaces] = useState(base?.id)
  const [pendingRebase, setPendingRebase] = useState<{ revision: number; version: Version } | null>(null)
  const save = useVersionWrite(cycle.id)
  const dirty =
    title !== (base?.title ?? '') ||
    change !== '' ||
    JSON.stringify(content) !== JSON.stringify(base?.content ?? {}) ||
    JSON.stringify(dependencies) !== JSON.stringify(base?.dependencies ?? [])
  const blocker = useNavigationBlocker(
    dirty && !save.isPending && !save.isSuccess,
  )
  useBeforeUnload((event) => {
    if (dirty && !save.isSuccess) event.preventDefault()
  })
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (save.isPending || fileLoading) return
    save.mutate(
      {
        expected_revision: revision,
        kind,
        title,
        content,
        dependencies,
        replaces,
        change_summary: change,
      },
      { onSuccess: (data) => onSaved(data.versions.at(-1)?.id ?? '') },
    )
  }
  const knownFields = new Set([
    ...(cycle.fields?.[kind]?.map((field) => field.key) ?? []),
    ...(kind === 'scope'
      ? ['_source_evidence']
      : kind === 'report'
        ? ['_execution_evidence']
        : []),
  ])
  const unsupported = Object.keys(content).filter(
    (key) => !knownFields.has(key),
  )
  const currentDependencies = cycle.versions.filter(
    (v) => v.approval_current && v.id !== base?.id,
  )
  return (
    <>
      <form
        className="cycle-editor"
        onSubmit={submit}
        aria-label="Deliverable editor"
      >
        <h2>
          {base ? 'Create a revised draft' : 'Create a deliverable draft'}
        </h2>
        <p>
          Each save creates an immutable version. Fields marked * must be
          complete before review; incomplete drafts can be saved.
        </p>
        <fieldset disabled={save.isPending || fileLoading}>
          <label className="cycle-field">
            Deliverable title
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              required
              maxLength={255}
            />
          </label>
          {kind === 'script' && (
            <div className="cycle-field">
              <label htmlFor="cycle-script-file">
                Import UTF-8 script source
              </label>
              <input
                id="cycle-script-file"
                type="file"
                accept=".json,.c,.txt"
                onChange={async (event) => {
                  const file = event.target.files?.[0]
                  event.target.value = ''
                  if (!file) return
                  setFileError('')
                  setFileLoading(true)
                  try {
                    if (file.size > 150000)
                      throw new Error(
                        'Script files must be under 150,000 bytes.',
                      )
                    const text = new TextDecoder('utf-8', {
                      fatal: true,
                    }).decode(await file.arrayBuffer())
                    if (
                      !text.trim() ||
                      text.includes('\0') ||
                      text.length > 50000
                    )
                      throw new Error(
                        'Use non-empty UTF-8 source under 50,000 characters, without NUL bytes.',
                      )
                    setContent((previous) => ({ ...previous, source: text }))
                    setFileName(file.name)
                  } catch (error) {
                    setFileError(
                      error instanceof Error
                        ? error.message
                        : 'Unable to read script source.',
                    )
                  } finally {
                    setFileLoading(false)
                  }
                }}
              />
              <small>
                Native JSON or Web HTTP/HTML source only. Choose the matching
                platform and format below. Importing replaces this draft’s
                source; save and approve before storage.
              </small>
              {fileLoading && <p role="status">Reading script source…</p>}
              {fileName && (
                <p role="status">Imported {fileName} into this draft.</p>
              )}
              {fileError && <p role="alert">{fileError}</p>}
            </div>
          )}
          {cycle.fields?.[kind]?.map((field) => (
            <FieldInput
              key={field.key}
              field={field}
              value={content[field.key]}
              onChange={(value) =>
                setContent((previous) => ({ ...previous, [field.key]: value }))
              }
            />
          ))}
          {kind === 'readiness' && (
            <div className="tonal-card info">
              <Link to={`/environments/${encodeURIComponent(cycle.environment_id)}`}>Review environment inventory</Link>
              {inventory.data?.snapshot?.id && <button className="btn btn-secondary" type="button" onClick={() =>
                setContent((previous) => ({ ...previous, inventory_snapshot_id: inventory.data!.snapshot!.id }))
              }>Use latest inventory snapshot</button>}
              {inventory.error && <p>Inventory could not be loaded. You can retain an existing snapshot reference.</p>}
            </div>
          )}
          {kind === 'test_plan' && <p className="tonal-card warning">Arrival-rate scheduling is not supported by pipeline execution. A plan with an arrival rate will be blocked at preflight.</p>}
          {unsupported.length > 0 && (
            <details>
              <summary>Unsupported fields from the previous version</summary>
              <p>
                These fields must be removed before review. The previous version
                will retain its original content.
              </p>
              <pre className="cycle-technical">
                {JSON.stringify(
                  Object.fromEntries(
                    unsupported.map((key) => [key, content[key]]),
                  ),
                  null,
                  2,
                )}
              </pre>
              <button
                className="btn btn-secondary"
                type="button"
                onClick={() =>
                  setContent((previous) =>
                    Object.fromEntries(
                      Object.entries(previous).filter(([key]) =>
                        knownFields.has(key),
                      ),
                    ),
                  )
                }
              >
                Remove unsupported fields from this draft
              </button>
            </details>
          )}
          <fieldset className="cycle-dependencies">
            <legend>Approved upstream evidence</legend>
            <p>
              Selected versions are frozen references. Changes to them will make
              this draft’s approval stale.
            </p>
            {currentDependencies.map((v) => (
              <label key={v.id}>
                <input
                  type="checkbox"
                  checked={dependencies.includes(v.id)}
                  onChange={(e) =>
                    setDependencies((previous) =>
                      e.target.checked
                        ? [...previous, v.id]
                        : previous.filter((id) => id !== v.id),
                    )
                  }
                />{' '}
                {v.title} · {v.kind.replaceAll('_', ' ')} · {v.id}
              </label>
            ))}
            {!currentDependencies.length && (
              <p>No approved upstream versions are available yet.</p>
            )}
            {dependencies
              .filter((id) => !currentDependencies.some((v) => v.id === id))
              .map((id) => (
                <label key={id}>
                  <input
                    type="checkbox"
                    checked
                    onChange={() =>
                      setDependencies((previous) =>
                        previous.filter((value) => value !== id),
                      )
                    }
                  />{' '}
                  Stale reference {id} — remove and select a current version.
                </label>
              ))}
          </fieldset>
          <label className="cycle-field">
            Change summary and affected requirements
            <textarea
              required
              value={change}
              onChange={(e) => setChange(e.target.value)}
              maxLength={2000}
              rows={3}
            />
          </label>
          <div className="cycle-actions">
            <button className="btn btn-primary" type="submit">
              {save.isPending ? 'Saving…' : 'Save draft version'}
            </button>
            <button
              className="btn btn-secondary"
              type="button"
              onClick={() => dirty ? setCancelRequested(true) : onCancel()}
            >
              Cancel editing
            </button>
          </div>
        </fieldset>
        {save.error && (
          <div role="alert" className="tonal-card warning">
            <p>{save.error.message}</p>
            <p>
              Your text is retained. Review the latest cycle revision before saving again.
            </p>
            {save.error instanceof ApiError && save.error.status === 409 && (
              <button className="btn btn-secondary" type="button" disabled={freshCycle.isFetching}
                onClick={async () => {
                  const result = await freshCycle.refetch()
                  if (result.data && !result.error) {
                    let latest = result.data.versions.find(version => version.id === replaces)
                    const visited = new Set<string>()
                    while (latest && !visited.has(latest.id)) {
                      visited.add(latest.id)
                      const next = result.data.versions.find(version => version.replaces === latest?.id)
                      if (!next) break
                      latest = next
                    }
                    if (latest && (latest.id !== replaces || latest.archived)) setPendingRebase({ revision: result.data.revision, version: latest })
                    else { setRevision(result.data.revision); save.reset() }
                  }
                }}>Keep my text and load the latest revision</button>
            )}
            {pendingRebase && <div className="tonal-card warning"><p>A newer version is available: <strong>{pendingRebase.version.title}</strong> ({pendingRebase.version.id}). Your text remains unchanged. Review it against the newer evidence before saving.</p><button type="button" className="btn btn-secondary" onClick={() => { setRevision(pendingRebase.revision); setReplaces(pendingRebase.version.archived ? undefined : pendingRebase.version.id); setPendingRebase(null); save.reset() }}>{pendingRebase.version.archived ? 'Keep my text as an independent draft' : 'Keep my text and revise the latest version'}</button></div>}
            {freshCycle.error && <p>Could not refresh the revision: {freshCycle.error.message}</p>}
          </div>
        )}
      </form>
      {(blocker.state === 'blocked' || cancelRequested) && (
        <Dialog
          ariaLabel="Unsaved deliverable changes"
          className="cycle-leave-dialog"
          overlayClassName="cycle-dialog-overlay"
          onClose={() => { setCancelRequested(false); if (blocker.state === 'blocked') blocker.reset() }}
        >
          <h2>Leave unsaved changes?</h2>
          <p>Your current edits have not been saved as a version.</p>
          <div className="cycle-actions">
            <button className="btn btn-primary" onClick={() => { setCancelRequested(false); if (blocker.state === 'blocked') blocker.reset() }}>
              Keep editing
            </button>
            <button
              className="btn btn-secondary"
              onClick={() => { if (cancelRequested) onCancel(); else if (blocker.state === 'blocked') blocker.proceed() }}
            >
              Discard and leave
            </button>
          </div>
        </Dialog>
      )}
    </>
  )
}
