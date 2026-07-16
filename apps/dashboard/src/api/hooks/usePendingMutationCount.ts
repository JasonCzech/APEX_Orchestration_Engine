import {
  hashKey,
  useMutationState,
  type Mutation,
  type MutationKey,
} from '@tanstack/react-query'

function selectMutationKeyHash(mutation: Mutation): string | null {
  const mutationKey = mutation.options.mutationKey
  return mutationKey ? hashKey(mutationKey) : null
}

/**
 * Counts pending mutations for one exact key.
 *
 * TanStack's useIsMutating selector refreshes when the mutation cache changes,
 * but a mounted detail route can change its filter without a cache event. By
 * subscribing to the stable set of all pending keys and matching the current
 * key during render, route switches cannot inherit a stale pending count.
 */
export function usePendingMutationCount(mutationKey: MutationKey): number {
  const targetHash = hashKey(mutationKey)
  const pendingKeyHashes = useMutationState<string | null>({
    filters: { status: 'pending' },
    select: selectMutationKeyHash,
  })
  return pendingKeyHashes.reduce(
    (count, pendingHash) => count + (pendingHash === targetHash ? 1 : 0),
    0,
  )
}
