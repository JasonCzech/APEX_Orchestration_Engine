import { useId, useRef, useState } from 'react'
import { Link } from 'react-router'
import type { components } from '@apex/api-client'
import { Dialog } from '@/components/Dialog'
import { CopyableId } from '@/components/ux/CopyableId'
import { Skeleton } from '@/components/ux/Skeleton'
import { formatTimestamp } from '@/utils/time'
import { useCancelExecutionDispatch, useDispatchExecution, useExecutionDispatch, type DispatchAttempt } from './dispatchApi'
import { CycleActorIdentity } from './CycleActorIdentity'

const statusLabels: Record<DispatchAttempt['status'], string> = {
  reserved: 'Dispatch reserved',
  submitted: 'Execution submitted',
  uncertain: 'Dispatch outcome unconfirmed',
  running: 'Execution running',
  cleanup_pending: 'Awaiting results and cleanup',
  completed: 'Execution complete',
  failed: 'Execution failed',
  aborted: 'Execution aborted',
}

export function ExecutionDispatch({ cycleId, preparation, environmentName, canEdit }: {
  cycleId: string
  preparation: components['schemas']['PreparationRead']
  environmentName: string
  canEdit: boolean
}) {
  const headingId = useId()
  const query = useExecutionDispatch(cycleId, preparation.id)
  const dispatch = useDispatchExecution(cycleId, preparation.id)
  const cancel = useCancelExecutionDispatch(cycleId, preparation.id)
  const requestKey = useRef<string | null>(null)
  const [confirmation, setConfirmation] = useState<{ revision: number; key: string; resume: boolean; reconcile: boolean } | null>(null)
  const [cancelId, setCancelId] = useState<string | null>(null)
  const data = query.data
  const attempt = data?.attempt
  const resume = Boolean(attempt && data?.can_resume)
  const reconcile = resume && Boolean(data?.checks.some(check => check.status === 'blocked'))
  const confirmationLabel = confirmation?.reconcile ? 'Reconcile launch' : confirmation?.resume ? 'Resume execution dispatch' : 'Launch approved execution'
  const available = canEdit && !preparation.archived_at && (resume || (!attempt && data?.can_dispatch))
  const terminal = attempt && ['completed', 'failed', 'aborted'].includes(attempt.status)
  const busy = dispatch.isPending || cancel.isPending

  function openConfirmation() {
    if (!data || !available) return
    // A receipt survives navigation and identifies the original operation. An
    // uncertain local request keeps its key until the server returns a receipt.
    requestKey.current = attempt?.request_key ?? requestKey.current ?? crypto.randomUUID().replaceAll('-', '')
    dispatch.reset()
    setConfirmation({ revision: data.revision, key: requestKey.current, resume, reconcile })
  }

  return <section aria-labelledby={headingId}>
    <h4 id={headingId}>Execution dispatch</h4>
    {query.isPending && <Skeleton label="Loading dispatch prerequisites" rows={2} />}
    {query.error && <p role="alert">{query.error.message} <button className="btn btn-secondary" onClick={() => void query.refetch()}>Refresh dispatch</button></p>}
    {data && <>
      <p role="status"><strong>{attempt ? attempt.status === 'aborted' && !attempt.thread_id ? 'Dispatch canceled' : statusLabels[attempt.status] : data.can_dispatch ? 'Ready for launch review' : 'Launch blocked'}</strong>{attempt ? ` · ${attempt.detail}` : ''}</p>
      {attempt && <>
        <p>Requested {formatTimestamp(attempt.created_at)}. Updated {formatTimestamp(attempt.updated_at)}.</p>
        {attempt.thread_id && <p><Link to={`/runs/${encodeURIComponent(attempt.thread_id)}/phases/execution`}>Open execution and results</Link></p>}
        <details><summary>Dispatch receipt</summary><dl>
          <dt>Operation</dt><dd><CopyableId value={attempt.id} label="Dispatch operation ID" /></dd>
          <dt>Requested by</dt><dd><CycleActorIdentity id={attempt.created_by} /></dd>
          {attempt.canceled_by && <><dt>Canceled by</dt><dd><CycleActorIdentity id={attempt.canceled_by} /></dd></>}
          <dt>Preparation</dt><dd><CopyableId value={preparation.id} label="Dispatch preparation ID" /></dd>
          {attempt.engine_run_id && <><dt>Engine attempt</dt><dd><CopyableId value={attempt.engine_run_id} label="Engine attempt ID" /></dd></>}
          {attempt.external_run_id && <><dt>Provider execution</dt><dd><CopyableId value={attempt.external_run_id} label="Provider execution ID" /></dd></>}
        </dl></details>
        {terminal && <p>Prepare a new execution after review to run again. This receipt remains attached to its original preparation.</p>}
      </>}
      {data.checks.some(check => check.status !== 'ready') && <ul aria-label="Dispatch prerequisites">
        {data.checks.filter(check => check.status !== 'ready').map(check => <li key={check.key}><strong>{check.label}</strong> — {check.detail}</li>)}
      </ul>}
      {canEdit && !preparation.archived_at && !terminal && (!attempt || resume) && <button className="btn btn-primary" disabled={!available || busy || query.isFetching} onClick={openConfirmation}>
        {reconcile ? 'Reconcile launch' : resume ? 'Resume dispatch' : 'Launch approved execution'}
      </button>}
      {canEdit && data.can_cancel && attempt && <button className="btn btn-danger" disabled={busy || query.isFetching} onClick={() => { cancel.reset(); setCancelId(attempt.id) }}>Cancel unsubmitted launch</button>}
      <button className="btn btn-secondary" disabled={query.isFetching || busy} onClick={() => void query.refetch()}>Refresh dispatch receipt</button>
      {!attempt && <p>Launching creates one execution from these approved bindings. Preparing or refreshing does not launch a test.</p>}
    </>}
    {confirmation && <Dialog ariaLabel={confirmationLabel} className="cycle-leave-dialog" overlayClassName="cycle-dialog-overlay" onClose={() => { if (!dispatch.isPending) setConfirmation(null) }} closeOnBackdrop={!dispatch.isPending} closeOnEscape={!dispatch.isPending}>
      <h2>{confirmationLabel}</h2>
      <p>{confirmation.reconcile ? 'Recover the original launch receipt. Current prerequisites block execution: the pipeline rechecks them before any provider action and records a failure if they remain blocked. This does not bypass review or permissions.' : confirmation.resume ? 'Recover the original dispatch using the same operation identity. Any confirmed execution remains the original execution.' : 'This starts a live test using the approved script and workload. Review the target and workload before launching.'}</p>
      <dl>
        <dt>Script</dt><dd>{preparation.snapshot.script_title}</dd>
        <dt>Plan</dt><dd>{preparation.snapshot.plan_title}</dd>
        <dt>Environment</dt><dd>{environmentName}</dd>
        <dt>Workload</dt><dd>{String(preparation.snapshot.workload.vusers)} VUsers · {String(preparation.snapshot.workload.ramp_s)}s ramp · {String(preparation.snapshot.workload.duration_s)}s duration</dd>
        <dt>Preparation</dt><dd><CopyableId value={preparation.id} label="Confirmed preparation ID" /></dd>
        <dt>Reviewed cycle revision</dt><dd>{confirmation.revision}</dd>
      </dl>
      <p>The server rechecks current approvals, provider bindings and exclusive use of this environment among cycle launches before dispatch.</p>
      {dispatch.error && <p role="alert">{dispatch.error.message} Close this dialog and refresh the receipt to review the current state.</p>}
      <div className="cycle-actions">
        <button className="btn btn-secondary" disabled={dispatch.isPending} onClick={() => setConfirmation(null)}>Cancel</button>
        <button className="btn btn-primary" disabled={dispatch.isPending || !available || Boolean(dispatch.error)} onClick={() => {
          dispatch.mutate({ expected_revision: confirmation.revision, idempotency_key: confirmation.key }, {
            onSuccess: () => setConfirmation(null),
          })
        }}>{dispatch.isPending ? 'Confirming dispatch…' : confirmation.reconcile ? 'Confirm reconciliation' : confirmation.resume ? 'Confirm resume dispatch' : 'Confirm launch'}</button>
      </div>
    </Dialog>}
    {cancelId && <Dialog ariaLabel="Cancel unsubmitted launch" className="cycle-leave-dialog" overlayClassName="cycle-dialog-overlay" onClose={() => { if (!cancel.isPending) setCancelId(null) }} closeOnBackdrop={!cancel.isPending} closeOnEscape={!cancel.isPending}>
      <h2>Cancel unsubmitted launch</h2>
      <p>The server cancels only while this intent has no bound pipeline. If submission has begun, cancellation is refused; open the linked execution to request a confirmed stop.</p>
      <p>The canceled intent remains in the audit history. A new execution requires a new preparation.</p>
      <CopyableId value={cancelId} label="Dispatch to cancel" />
      {cancel.error && <p role="alert">{cancel.error.message} Refresh the receipt before continuing.</p>}
      <div className="cycle-actions">
        <button className="btn btn-secondary" disabled={cancel.isPending} onClick={() => setCancelId(null)}>Keep launch intent</button>
        <button className="btn btn-danger" disabled={cancel.isPending || Boolean(cancel.error) || !canEdit || !data?.can_cancel || attempt?.id !== cancelId} onClick={() => cancel.mutate({ dispatch_id: cancelId }, { onSuccess: () => setCancelId(null) })}>{cancel.isPending ? 'Canceling…' : 'Confirm cancel launch'}</button>
      </div>
    </Dialog>}
  </section>
}
