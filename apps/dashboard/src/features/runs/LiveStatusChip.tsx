import { isKnown, KNOWN } from './liveStatusDisplay'

/**
 * Header chip showing the SSE connection state (D2). Plain status text +
 * tone-colored pulse dot; the title attribute explains each state. Unknown
 * statuses (future streaming-layer states) render muted with the raw text.
 * RunAnnouncements owns the one live region for gate, phase and transport changes.
 */
export function LiveStatusChip({ status }: { status: string }) {
  const known = isKnown(status) ? KNOWN[status] : null
  return (
    <span
      className={`live-status-chip ${isKnown(status) ? status : 'idle'}`}
      data-testid="live-status-chip"
      data-status={status}
      title={known?.title ?? `Stream status: ${status}`}
    >
      <span className="live-status-dot" aria-hidden="true" />
      {known?.label ?? status}
    </span>
  )
}
