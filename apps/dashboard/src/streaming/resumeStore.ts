/**
 * Last-SSE-event-id store per (threadId, runId), backing joinStream resume
 * (plan Part 2: kill the tab mid-run, reopen, state intact). sessionStorage:
 * survives reloads within the tab, scoped per tab so two operators watching
 * the same run never share a cursor. All accessors swallow storage errors —
 * resume is a liveness optimization, never a correctness dependency
 * (snapshot + tail heals regardless).
 */
const PREFIX = 'apex:stream:last-event-id'
export const MAX_RESUME_EVENT_ID_CHARS = 1_024

function isValidEventId(eventId: string): boolean {
  // Last-Event-ID becomes an HTTP request header. Keep only a bounded visible
  // ASCII token; malformed/oversized server ids fall back to snapshot healing
  // instead of poisoning every reconnect with an invalid or enormous header.
  return (
    eventId.length > 0 &&
    eventId.length <= MAX_RESUME_EVENT_ID_CHARS &&
    /^[\x21-\x7e]+$/.test(eventId)
  )
}

function storageKey(threadId: string, runId: string): string {
  return `${PREFIX}:${JSON.stringify([threadId, runId])}`
}

export const resumeStore = {
  get(threadId: string, runId: string): string | null {
    try {
      const eventId = window.sessionStorage.getItem(storageKey(threadId, runId))
      if (eventId === null || isValidEventId(eventId)) return eventId
      window.sessionStorage.removeItem(storageKey(threadId, runId))
      return null
    } catch {
      return null
    }
  },
  set(threadId: string, runId: string, eventId: string): void {
    try {
      if (!isValidEventId(eventId)) {
        window.sessionStorage.removeItem(storageKey(threadId, runId))
        return
      }
      window.sessionStorage.setItem(storageKey(threadId, runId), eventId)
    } catch {
      // storage full/unavailable: lose resume, keep streaming
    }
  },
  clear(threadId: string, runId: string): void {
    try {
      window.sessionStorage.removeItem(storageKey(threadId, runId))
    } catch {
      // ignore
    }
  },
}
