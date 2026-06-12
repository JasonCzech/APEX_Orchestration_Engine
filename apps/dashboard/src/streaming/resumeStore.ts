/**
 * Last-SSE-event-id store per (threadId, runId), backing joinStream resume
 * (plan Part 2: kill the tab mid-run, reopen, state intact). sessionStorage:
 * survives reloads within the tab, scoped per tab so two operators watching
 * the same run never share a cursor. All accessors swallow storage errors —
 * resume is a liveness optimization, never a correctness dependency
 * (snapshot + tail heals regardless).
 */
const PREFIX = 'apex:stream:last-event-id'

function storageKey(threadId: string, runId: string): string {
  return `${PREFIX}:${threadId}:${runId}`
}

export const resumeStore = {
  get(threadId: string, runId: string): string | null {
    try {
      return window.sessionStorage.getItem(storageKey(threadId, runId))
    } catch {
      return null
    }
  },
  set(threadId: string, runId: string, eventId: string): void {
    try {
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
