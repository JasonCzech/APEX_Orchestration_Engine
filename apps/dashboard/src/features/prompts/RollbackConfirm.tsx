/**
 * Confirm modal for the rollback pointer move (detail versions tab + version
 * page). Shows the target version number and its note so the operator sees
 * exactly what becomes active.
 */
import { isApiError } from '@/api/errors'
import { Dialog } from '@/components/Dialog'

export function RollbackConfirm({
  version,
  note,
  pending,
  error,
  onConfirm,
  onCancel,
}: {
  version: number
  note: string | null | undefined
  pending: boolean
  error: unknown
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <Dialog
      overlayClassName="prompt-modal-overlay"
      className="prompt-modal prompt-modal-narrow glass-panel"
      ariaLabel={`Set v${version} active`}
      onClose={onCancel}
      closeOnBackdrop={!pending}
      closeOnEscape={!pending}
    >
      <h2 className="prompt-modal-title">Set v{version} active?</h2>
        <p className="prompt-modal-caption">
          The active pointer moves to <strong>v{version}</strong>
          {note ? (
            <>
              {' '}
              — <em>{note}</em>
            </>
          ) : null}
          . No versions are modified or deleted.
        </p>
        {Boolean(error) && (
          <div className="tonal-card danger" role="alert">
            {isApiError(error)
              ? error.message
              : error instanceof Error
                ? error.message
                : 'Rollback failed.'}
          </div>
        )}
        <div className="prompt-modal-actions">
          <button type="button" className="btn btn-ghost" onClick={onCancel} disabled={pending}>
            Cancel
          </button>
          <button type="button" className="btn btn-primary" onClick={onConfirm} disabled={pending}>
            {pending ? 'Setting active…' : `Set v${version} active`}
          </button>
        </div>
    </Dialog>
  )
}
