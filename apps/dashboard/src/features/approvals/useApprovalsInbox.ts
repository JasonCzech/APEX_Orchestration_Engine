/**
 * Approvals inbox data layer (plan UX 2.b).
 *
 * Reuses usePipelines — the same 15s visibility-aware poll on
 * queryKeys.pipelines.list — with a fixed `{status: 'interrupted', limit: 100}`
 * filter, then derives the gate queue client-side from `pending_gate`. Because
 * the Sidebar badge and the inbox page both call this hook with the same
 * constant filter, react-query dedupes them onto ONE cache entry / ONE poll.
 *
 * Also tracks rows that vanish between polls ("actioned elsewhere"): a row
 * present in the previous successful response but missing from the latest one
 * is surfaced via `removedItems` for exactly one poll cycle, so the page can
 * gray it inline instead of yanking it from the queue mid-review.
 */
import { useRef } from 'react'

import type { PendingGate } from '@/api/hooks/usePipelines'
import { usePipelines } from '@/api/hooks/usePipelines'
import { formatRelative } from '@/utils/time'

/** A gate older than this pulses amber in the queue + sidebar badge. */
export const STALE_GATE_MS = 15 * 60_000

/** Single page scan — the backend caps limit at 100 (runs ROUTES.md contract). */
const INBOX_SCAN_LIMIT = 100

/** Fixed filter — keep referentially stable semantics for query-key dedupe. */
const INBOX_FILTER = { status: 'interrupted' as const, limit: INBOX_SCAN_LIMIT }

export interface ApprovalItem {
  thread_id: string
  title: string
  project_id: string | null
  pending_gate: PendingGate
  updated_at: string | null
  /** formatRelative(updated_at) — "5m ago" etc. */
  age: string
  /** True when the gate has been waiting longer than STALE_GATE_MS. */
  isStale: boolean
}

export interface ApprovalsInboxResult {
  /** Pending gates, oldest-updated first. */
  items: ApprovalItem[]
  /**
   * Rows seen in the previous poll that disappeared in the latest one —
   * resumed elsewhere (or by this tab). Retained for one poll cycle.
   */
  removedItems: ApprovalItem[]
  count: number
  /** Any pending gate older than STALE_GATE_MS (drives badge pulse). */
  hasStale: boolean
  isLoading: boolean
  error: Error | null
  refetch: () => void
}

function toItem(
  row: {
    thread_id: string
    title?: string | null
    project_id?: string | null
    pending_gate?: PendingGate | null
    updated_at?: string | null
  },
  now: number,
): ApprovalItem {
  const updatedMs = row.updated_at ? Date.parse(row.updated_at) : Number.NaN
  return {
    thread_id: row.thread_id,
    title: row.title || 'Untitled run',
    project_id: row.project_id ?? null,
    pending_gate: row.pending_gate as PendingGate,
    updated_at: row.updated_at ?? null,
    age: formatRelative(row.updated_at, now),
    isStale: Number.isFinite(updatedMs) && now - updatedMs > STALE_GATE_MS,
  }
}

export function useApprovalsInbox(): ApprovalsInboxResult {
  const query = usePipelines(INBOX_FILTER)

  // Derivations are cheap (≤100 rows); recompute per render so ages stay
  // current without extra timers. Sorting: oldest updated_at first (nulls
  // last) — the longest-waiting gate is the queue head.
  const now = Date.now()
  const items = (query.data?.items ?? [])
    .filter((row) => row.pending_gate != null)
    .map((row) => toItem(row, now))
    .sort((a, b) => {
      if (a.updated_at === b.updated_at) return a.thread_id.localeCompare(b.thread_id)
      if (a.updated_at === null) return 1
      if (b.updated_at === null) return -1
      return a.updated_at.localeCompare(b.updated_at)
    })

  // "Actioned elsewhere" tracking: diff against the previous successful poll.
  // Ref mutation is keyed on dataUpdatedAt, so it is idempotent per snapshot
  // (safe under double-rendering).
  const snapshot = useRef<{ updatedAt: number; items: ApprovalItem[]; removed: ApprovalItem[] }>({
    updatedAt: 0,
    items: [],
    removed: [],
  })
  if (query.data && query.dataUpdatedAt !== snapshot.current.updatedAt) {
    const currentIds = new Set(items.map((item) => item.thread_id))
    snapshot.current = {
      updatedAt: query.dataUpdatedAt,
      items,
      removed: snapshot.current.items.filter((prev) => !currentIds.has(prev.thread_id)),
    }
  }

  return {
    items,
    removedItems: snapshot.current.removed,
    count: items.length,
    hasStale: items.some((item) => item.isStale),
    isLoading: query.isPending,
    error: query.isError ? query.error : null,
    refetch: () => void query.refetch(),
  }
}
