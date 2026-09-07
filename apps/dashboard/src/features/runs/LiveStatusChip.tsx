import type { LiveStreamStatus } from './liveTypes'

const KNOWN: Record<LiveStreamStatus, { label: string; title: string }> = {
  idle: {
    label: 'Live updates paused',
    title: 'No live stream — this run is not currently streaming. The snapshot poll keeps the page current.',
  },
  connecting: {
    label: 'Connecting live updates',
    title: 'Opening the live event stream for this run…',
  },
  live: {
    label: 'Live updates connected',
    title: 'Live — events from this run stream in as they happen.',
  },
  reconnecting: {
    label: 'Reconnecting live updates',
    title: 'Stream dropped — reconnecting with backoff. The snapshot poll heals any missed events.',
  },
  ended: {
    label: 'Live updates ended',
    title: 'The run finished streaming. The snapshot below is the durable record.',
  },
  error: {
    label: 'Live updates unavailable',
    title: 'The live stream failed. The snapshot poll keeps the page correct; reload to retry the stream.',
  },
}

function isKnown(status: string): status is LiveStreamStatus {
  return status in KNOWN
}

/**
 * Header chip showing the SSE connection state (D2). Plain status text +
 * tone-colored pulse dot; the title attribute explains each state. Unknown
 * statuses (future streaming-layer states) render muted with the raw text.
 * Deliberately NOT role="status": the run rail's gate banner owns that
 * landmark on this page (and the chip's churn would spam screen readers).
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
