/**
 * Approvals inbox data layer (plan UX 2.b).
 *
 * Uses the pipelines list cache family and the same 15s visibility-aware poll,
 * but walks every 100-row page before publishing the snapshot. Because the
 * Sidebar badge and inbox page use the same key, React Query dedupes them onto
 * one paginated scan/poll.
 *
 * Also tracks rows that vanish between polls ("actioned elsewhere"): a row
 * present in the previous successful response but missing from the latest one
 * is surfaced via `removedItems` for exactly one poll cycle, so the page can
 * gray it inline instead of yanking it from the queue mid-review.
 */
import { useRef } from 'react'

import { useQuery } from '@tanstack/react-query'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import {
  PIPELINES_POLL_INTERVAL_MS,
  type PendingGate,
  type PipelineListResponse,
  type PipelineSummary,
} from '@/api/hooks/usePipelines'
import { queryKeys, STALE_TIMES } from '@/api/queryKeys'
import { formatRelative } from '@/utils/time'

/** A gate older than this pulses amber in the queue + sidebar badge. */
export const STALE_GATE_MS = 15 * 60_000

/** Backend page cap (runs ROUTES.md contract). */
const INBOX_SCAN_LIMIT = 100

/** Fixed filter — keep referentially stable semantics for query-key dedupe. */
const INBOX_FILTER = { status: 'interrupted' as const, limit: INBOX_SCAN_LIMIT }

async function fetchAllInterruptedPipelines(): Promise<PipelineListResponse> {
  const items: PipelineSummary[] = []
  const seen = new Set<string>()
  let offset = 0

  for (;;) {
    const { data, error, response } = await getApexClient().GET('/v1/pipelines', {
      params: {
        query: { status: INBOX_FILTER.status, limit: INBOX_SCAN_LIMIT, offset },
      },
    })
    if (!response.ok || !data) {
      throw new ApiError(
        response.status,
        errorMessageOf(error, `Approvals request failed (${response.status})`),
        error,
      )
    }

    const page = data as PipelineListResponse
    let added = 0
    for (const row of page.items) {
      if (seen.has(row.thread_id)) continue
      seen.add(row.thread_id)
      items.push(row)
      added += 1
    }

    offset += page.items.length
    const total = page.total
    if (
      page.items.length < INBOX_SCAN_LIMIT ||
      page.items.length === 0 ||
      added === 0 ||
      (total !== undefined && offset >= total)
    ) {
      return { items, limit: INBOX_SCAN_LIMIT, offset: 0, ...(total !== undefined ? { total } : {}) }
    }
  }
}

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
  const query = useQuery({
    queryKey: queryKeys.pipelines.list(INBOX_FILTER),
    queryFn: fetchAllInterruptedPipelines,
    staleTime: STALE_TIMES.pipelinesList,
    refetchInterval: PIPELINES_POLL_INTERVAL_MS,
    refetchIntervalInBackground: false,
  })

  // Derivations are cheap; recompute per render so ages stay
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
