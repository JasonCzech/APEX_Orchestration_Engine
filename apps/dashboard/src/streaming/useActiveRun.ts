/**
 * Active-run discovery for a thread (plan Part 2 — Data layer).
 *
 * SDK surface (verified in node_modules/@langchain/langgraph-sdk
 * dist/client/runs/index.d.ts): `client.runs.list(threadId, { limit, offset,
 * status?, signal? })` → `Run[]` with `run_id` and `status` ∈ pending |
 * running | error | success | timeout | interrupted. `status` accepts ONE
 * value per call, so we fetch the recent page once and pick running → pending
 * client-side instead of issuing two requests.
 *
 * Poll policy: lightweight 5s poll only while the thread is busy (caller
 * passes the snapshot's thread_status) — or, when the status is unknown,
 * while a live run is actually in hand. Cross-run liveness stays poll-based
 * per the plan; this hook only feeds the per-run stream.
 */
import { useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { getLangGraphClient } from '@/api/langgraphClient'
import { queryKeys } from '@/api/queryKeys'

export const ACTIVE_RUN_POLL_MS = 5_000
export const ACTIVE_RUN_SCAN_LIMIT = 20

/** Run statuses that mean "this run is (about to be) streaming". */
const ACTIVE_RUN_STATUSES = ['running', 'pending'] as const

async function fetchActiveRunId(threadId: string, signal?: AbortSignal): Promise<string | null> {
  const client = await getLangGraphClient()
  const runs = await client.runs.list(threadId, {
    limit: ACTIVE_RUN_SCAN_LIMIT,
    select: ['run_id', 'status'],
    signal,
  })
  for (const status of ACTIVE_RUN_STATUSES) {
    const match = runs.find((run) => run.status === status)
    if (match) return match.run_id
  }
  return null
}

export interface UseActiveRunOptions {
  /**
   * Last known thread status ('busy' | 'idle' | 'interrupted' | 'error').
   * Omit/undefined = unknown → probe once, keep polling only while an active
   * run is found. 'busy' → poll. Anything else → disabled, returns null.
   */
  threadStatus?: string | null
}

export function useActiveRun(
  threadId: string | undefined,
  options: UseActiveRunOptions = {},
): string | null {
  const queryClient = useQueryClient()
  const statusKnown = options.threadStatus !== undefined
  const busy = options.threadStatus === 'busy'
  const enabled = Boolean(threadId) && (!statusKnown || busy)

  const query = useQuery({
    queryKey: queryKeys.threads.activeRun(threadId ?? ''),
    queryFn: ({ signal }) => fetchActiveRunId(threadId ?? '', signal),
    enabled,
    staleTime: 0,
    refetchInterval: (q) => {
      if (!enabled) return false
      if (busy) return ACTIVE_RUN_POLL_MS
      // Status unknown: keep tracking only while a live run is in hand.
      return q.state.data ? ACTIVE_RUN_POLL_MS : false
    },
    refetchIntervalInBackground: false,
  })

  useEffect(() => {
    if (!enabled && threadId) {
      void queryClient.cancelQueries({ queryKey: queryKeys.threads.activeRun(threadId) })
      queryClient.setQueryData(queryKeys.threads.activeRun(threadId), null)
    }
  }, [enabled, threadId, queryClient])

  if (!enabled) return null
  return query.data ?? null
}
