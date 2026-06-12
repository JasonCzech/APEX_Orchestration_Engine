import type { LiveStreamStatus } from './liveTypes'

const KNOWN: Record<LiveStreamStatus, { label: string; title: string }> = {
  idle: {
    label: 'idle',
    title: 'No live stream — this run is not currently streaming. The snapshot poll keeps the page current.',
  },
  connecting: {
    label: 'connecting',
    title: 'Opening the live event stream for this run…',
  },
  live: {
    label: 'live',
    title: 'Live — events from this run stream in as they happen.',
  },
  reconnecting: {
    label: 'reconnecting',
    title: 'Stream dropped — reconnecting with backoff. The snapshot poll heals any missed events.',
  },
  ended: {
    label: 'stream ended',
    title: 'The run finished streaming. The snapshot below is the durable record.',
  },
  error: {
    label: 'stream error',
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
