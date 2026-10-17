import { useRef, useState } from 'react'
import { Link, useBeforeUnload } from 'react-router'
import { Dialog } from '@/components/Dialog'
import { useNavigationBlocker } from '@/components/useNavigationBlocker'
import { useCycleFollowup, useNextCycle, type Cycle } from './api'

export function CycleHandoff({
  cycle,
  canEdit,
}: {
  cycle: Cycle
  canEdit: boolean
}) {
  const save = useCycleFollowup(cycle.id),
    next = useNextCycle(cycle.id)
  const [actionRevision, setActionRevision] = useState<number | null>(null)
  const [actionId, setActionId] = useState<string | null>(null)
  const [title, setTitle] = useState(''),
    [owner, setOwner] = useState(''),
    [note, setNote] = useState('')
  const [status, setStatus] = useState<
    'open' | 'in_progress' | 'done' | 'cancelled'
  >('open')
  const [name, setName] = useState(''),
    [selected, setSelected] = useState<string[]>([]),
    [carry, setCarry] = useState(true)
  const actionKey = useRef<{ signature: string; key: string } | null>(null),
    nextKey = useRef<{ signature: string; key: string } | null>(null)
  const actionDirty = Boolean(title || owner || note)
  const dirty =
    Boolean(title || owner || note || name || selected.length) &&
    !next.isSuccess
  const guard = useNavigationBlocker(dirty)
  useBeforeUnload((e) => {
    if (dirty) e.preventDefault()
  })
  const busy = save.isPending || next.isPending
  const candidates = cycle.versions.filter(
    (v) =>
      ['scope', 'test_plan', 'script'].includes(v.kind) &&
      !v.archived &&
      !cycle.versions.some((other) => other.replaces === v.id),
  )
  function clearAction() {
    setActionId(null)
    setActionRevision(null)
    setTitle('')
    setOwner('')
    setNote('')
    setStatus('open')
  }
  return (
    <section
      className="cycle-panel glass-panel"
      aria-label="Cycle follow-up work"
    >
      <h3>Follow-up actions</h3>
      {!cycle.followups?.length && <p>No tracked actions yet.</p>}
      <ul>
        {cycle.followups?.map((action) => (
          <li key={String(action.id)} className="cycle-panel">
            <strong>{String(action.title)}</strong>
            <p>
              Owner: {String(action.owner)} ·{' '}
              {String(action.status).replaceAll('_', ' ')}
            </p>
            <p>{String(action.note ?? '')}</p>
            {canEdit && (
              <button
                className="btn btn-secondary"
                disabled={busy || actionDirty}
                onClick={() => {
                  setActionRevision(cycle.revision)
                  setActionId(String(action.id))
                  setTitle(String(action.title))
                  setOwner(String(action.owner))
                  setStatus(action.status as typeof status)
                  setNote(String(action.note ?? ''))
                }}
              >
                Edit action
              </button>
            )}
            <details>
              <summary>Action history</summary>
              <pre className="cycle-technical">
                {JSON.stringify(action.history, null, 2)}
              </pre>
            </details>
          </li>
        ))}
      </ul>
      {canEdit && (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            const logical = { action_id: actionId, title, owner, status, note }
            const signature = JSON.stringify(logical)
            if (actionKey.current?.signature !== signature)
              actionKey.current = {
                signature,
                key: crypto.randomUUID().replaceAll('-', ''),
              }
            save.mutate(
              {
                ...logical,
                expected_revision: actionRevision ?? cycle.revision,
                idempotency_key: actionKey.current.key,
              },
              { onSuccess: () => {
                actionKey.current = null
                clearAction()
              } },
            )
          }}
        >
          <fieldset disabled={busy} className="cycle-execution-fields">
            <legend>{actionId ? 'Update action' : 'Add action'}</legend>
            <label className="cycle-field">
              Action title
              <input
                required
                maxLength={500}
                value={title}
                onChange={(e) => setTitle(e.target.value)}
              />
            </label>
            <label className="cycle-field">
              Action owner
              <input
                required
                maxLength={255}
                value={owner}
                onChange={(e) => setOwner(e.target.value)}
              />
            </label>
            <label className="cycle-field">
              Action status
              <select
                value={status}
                onChange={(e) => setStatus(e.target.value as typeof status)}
              >
                <option value="open">Open</option>
                <option value="in_progress">In progress</option>
                <option value="done">Done</option>
                <option value="cancelled">Cancelled</option>
              </select>
            </label>
            <label className="cycle-field">
              Progress and remediation notes
              <textarea
                maxLength={2000}
                value={note}
                onChange={(e) => setNote(e.target.value)}
              />
            </label>
            <button className="btn btn-primary" type="submit">
              {save.isPending ? 'Saving…' : 'Save action'}
            </button>
            {actionDirty && (
              <button
                type="button"
                className="btn btn-secondary"
                onClick={clearAction}
              >
                Clear action form
              </button>
            )}
          </fieldset>
        </form>
      )}
      {save.error && (
        <p role="alert">
          {save.error.message} Your entries are retained. If the cycle changed,
          copy your edits, clear the form, refresh the cycle and reopen the
          action before retrying.
        </p>
      )}
      {save.isSuccess && <p role="status">Action saved.</p>}
      <h3>Start next cycle</h3>
      {actionDirty && (
        <p>Save or clear the action form before creating the next cycle.</p>
      )}
      <p>
        Carry selected scope, plans and scripts as drafts. Every upstream
        dependency must be selected. Readiness, execution links, report evidence
        and approvals are not carried forward. Revalidate sources, target,
        workload and approvals before execution.
      </p>
      {cycle.origin_cycle_id && (
        <p>
          Continues{' '}
          <Link to={`/cycles/${encodeURIComponent(cycle.origin_cycle_id)}`}>
            the previous cycle
          </Link>
          .
        </p>
      )}
      {canEdit && (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            const logical = {
              name,
              version_ids: selected,
              carry_unresolved_actions: carry,
            }
            const signature = JSON.stringify(logical)
            if (nextKey.current?.signature !== signature)
              nextKey.current = {
                signature,
                key: crypto.randomUUID().replaceAll('-', ''),
              }
            next.mutate({
              ...logical,
              expected_revision: cycle.revision,
              idempotency_key: nextKey.current.key,
            })
          }}
        >
          <fieldset
            disabled={busy || next.isSuccess || actionDirty}
            className="cycle-execution-fields"
          >
            <legend>Next-cycle inputs</legend>
            <label className="cycle-field">
              Next cycle name
              <input
                required
                maxLength={255}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
            {candidates.map((v) => (
              <label key={v.id}>
                <input
                  type="checkbox"
                  checked={selected.includes(v.id)}
                  onChange={(e) =>
                    setSelected(
                      e.target.checked
                        ? [...selected, v.id]
                        : selected.filter((id) => id !== v.id),
                    )
                  }
                />{' '}
                {v.title} · {v.kind.replaceAll('_', ' ')} · {v.status}
              </label>
            ))}
            <label>
              <input
                type="checkbox"
                checked={carry}
                onChange={(e) => setCarry(e.target.checked)}
              />{' '}
              Carry unresolved actions as open work
            </label>
            <button className="btn btn-primary" type="submit">
              {next.isPending ? 'Creating…' : 'Create next cycle'}
            </button>
          </fieldset>
        </form>
      )}
      {next.error && (
        <p role="alert">{next.error.message} Selected inputs are retained.</p>
      )}
      {next.data && (
        <p role="status">
          Next cycle created with fresh review requirements.{' '}
          <Link to={`/cycles/${encodeURIComponent(next.data.id)}`}>
            Open {next.data.name}
          </Link>
        </p>
      )}
      {guard.state === 'blocked' && (
        <Dialog
          ariaLabel="Unsaved cycle follow-up"
          className="cycle-leave-dialog"
          overlayClassName="cycle-dialog-overlay"
          onClose={() => guard.reset()}
        >
          <h2>Leave unsaved follow-up inputs?</h2>
          <p>The follow-up inputs have not been saved to a cycle.</p>
          <div className="ux-dialog-actions">
            <button className="btn btn-primary" onClick={() => guard.reset()}>Keep editing</button>
            <button className="btn btn-secondary" onClick={() => guard.proceed()}>Discard and leave</button>
          </div>
        </Dialog>
      )}
    </section>
  )
}
