import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useBeforeUnload, useBlocker } from 'react-router'

import {
  acknowledgeConsumerKeyHandoff,
  getConsumerKeyHandoffSnapshot,
  invalidateConsumerKeyHandoffLifecycle,
  subscribeConsumerKeyHandoff,
  type ConsumerKeyHandoff,
} from '@/auth/consumerKeyHandoff'
import { setApiKey } from '@/auth/keyStorage'
import { Dialog } from '@/components/Dialog'

import './admin.css'

function KeyRevealModal({
  handoff,
  onAcknowledge,
  onUseKey,
}: {
  handoff: ConsumerKeyHandoff
  onAcknowledge: () => void
  onUseKey: () => void
}) {
  const [stored, setStored] = useState(false)
  const [copied, setCopied] = useState(false)
  const rotated = handoff.kind === 'rotated'

  function copy() {
    void navigator.clipboard?.writeText(handoff.created.api_key).then(
      () => setCopied(true),
      () => setCopied(false),
    )
  }

  return (
    <Dialog
      overlayClassName="adm-overlay"
      className="adm-modal glass-panel"
      ariaLabel={rotated ? 'API key rotated' : 'API key created'}
      onClose={() => undefined}
      closeOnBackdrop={false}
      closeOnEscape={false}
    >
      <h2 className="adm-panel-title">{rotated ? 'API key rotated' : 'API key created'}</h2>
      <p className="adm-modal-caption">
        API key for <strong>{handoff.created.name}</strong>. Store it now — it will never be shown
        again.
      </p>
      {rotated && (
        <p className="adm-modal-caption">
          The previous key remains valid for five minutes so clients can switch safely.
        </p>
      )}
      <div className="adm-key-row">
        <code className="adm-key" data-testid="revealed-api-key">
          {handoff.created.api_key}
        </code>
        <button type="button" className="btn btn-secondary btn-sm" onClick={copy}>
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <label className="adm-confirm-check">
        <input
          type="checkbox"
          checked={stored}
          onChange={(event) => setStored(event.target.checked)}
          aria-label="I have stored this key somewhere safe"
        />
        <span>I have stored this key somewhere safe</span>
      </label>
      <div className="adm-panel-actions">
        {rotated && handoff.isCurrentConsumer && (
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={!stored}
            onClick={onUseKey}
          >
            Use this key for this dashboard (recommended)
          </button>
        )}
        <button
          type="button"
          className="btn btn-primary btn-sm"
          disabled={!stored}
          onClick={onAcknowledge}
        >
          I&rsquo;ve stored it
        </button>
      </div>
    </Dialog>
  )
}

/**
 * App-shell lifetime owner for exactly-once consumer keys. The external store
 * survives route unmounts, while auth/session changes and shell teardown clear
 * it synchronously and invalidate every late mutation callback.
 */
export function ConsumerKeyLifecycle() {
  const snapshot = useSyncExternalStore(
    subscribeConsumerKeyHandoff,
    getConsumerKeyHandoffSnapshot,
    getConsumerKeyHandoffSnapshot,
  )
  const protectedState = snapshot.pending.length > 0 || snapshot.handoffs.length > 0
  const blocker = useBlocker(protectedState)
  const handoff = snapshot.handoffs[0] ?? null
  const proceedRequestedRef = useRef(false)

  const proceedBlockedNavigation = useCallback(() => {
    if (blocker.state !== 'blocked' || proceedRequestedRef.current) return
    proceedRequestedRef.current = true
    blocker.proceed()
  }, [blocker])

  useEffect(
    () => () => {
      invalidateConsumerKeyHandoffLifecycle()
    },
    [],
  )

  useBeforeUnload(
    useCallback(
      (event) => {
        if (!protectedState) return
        event.preventDefault()
        event.returnValue = ''
      },
      [protectedState],
    ),
  )

  useEffect(() => {
    if (blocker.state === 'unblocked') proceedRequestedRef.current = false
  }, [blocker.state])

  // A failed request leaves no secret to protect. Complete the navigation the
  // operator already requested instead of stranding a now-unnecessary blocker.
  useEffect(() => {
    if (!protectedState) proceedBlockedNavigation()
  }, [proceedBlockedNavigation, protectedState])

  function acknowledge(useKey: boolean) {
    if (!handoff) return
    const remainsProtected = snapshot.pending.length > 0 || snapshot.handoffs.length > 1
    acknowledgeConsumerKeyHandoff(handoff.id)
    if (!remainsProtected) proceedBlockedNavigation()
    if (useKey) {
      setApiKey(handoff.created.api_key)
    }
  }

  return (
    <>
      {snapshot.pending.length > 0 && handoff === null && blocker.state !== 'blocked' && (
        <div className="adm-key-lifecycle-status glass-panel" role="status">
          <strong>API key request in progress</strong>
          <span>Keep this tab open so the one-time key can be revealed safely.</span>
        </div>
      )}

      {handoff && (
        <KeyRevealModal
          key={handoff.id}
          handoff={handoff}
          onAcknowledge={() => acknowledge(false)}
          onUseKey={() => acknowledge(true)}
        />
      )}

      {blocker.state === 'blocked' && handoff === null && (
        <Dialog
          overlayClassName="adm-overlay"
          className="adm-modal glass-panel"
          ariaLabel="API key request still pending"
          onClose={blocker.reset}
          closeOnBackdrop={false}
        >
          <h2 className="adm-panel-title">API key request still pending</h2>
          <p className="adm-modal-caption">
            Leaving this screen will not cancel the server request. Keep this tab open: the
            resulting key is shown only once and will appear wherever you navigate in this
            dashboard.
          </p>
          <div className="adm-panel-actions">
            <button type="button" className="btn btn-secondary btn-sm" onClick={blocker.reset}>
              Stay here
            </button>
            <button type="button" className="btn btn-primary btn-sm" onClick={blocker.proceed}>
              Leave anyway
            </button>
          </div>
        </Dialog>
      )}
    </>
  )
}
