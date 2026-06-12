/**
 * Superseded gate banner (machine tag 'superseded').
 * - by 'conflict': the CAS resume 409'd — another operator (or surface)
 *   resumed this gate first.
 * - by 'cleared': the pending interrupt vanished from the snapshot without us
 *   resuming it — actioned elsewhere or replaced.
 * [View current state] = refetch + RESET (useGate.viewCurrent).
 */
export function SupersededBanner({
  by,
  onViewCurrent,
}: {
  by: 'conflict' | 'cleared'
  onViewCurrent?: (() => void) | undefined
}) {
  return (
    <div className="gate-superseded" data-testid="gate-superseded" data-by={by}>
      <span className="gate-superseded-title">
        {by === 'conflict'
          ? 'Another operator resumed this gate'
          : 'Gate actioned elsewhere or replaced'}
      </span>
      <span className="gate-superseded-detail">
        This review is no longer pending — your draft was not applied.
      </span>
      {onViewCurrent && (
        <button type="button" className="btn btn-secondary btn-sm" onClick={onViewCurrent}>
          View current state
        </button>
      )}
    </div>
  )
}
