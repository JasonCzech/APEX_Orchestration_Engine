import type { LiveStreamStatus } from './liveTypes'

export const KNOWN: Record<LiveStreamStatus, { label: string; title: string }> = {
  idle: {
    label: 'Snapshot refresh active',
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
    label: 'Recorded snapshot',
    title: 'The run finished streaming. The snapshot below is the durable record.',
  },
  error: {
    label: 'Live updates unavailable',
    title: 'The live stream failed. The snapshot poll keeps the page correct; reload to retry the stream.',
  },
}

export function isKnown(status: string): status is LiveStreamStatus {
  return status in KNOWN
}

export function liveStatusLabel(status: string): string {
  return isKnown(status) ? KNOWN[status].label : `Stream status: ${status}`
}
