import { createContext, useContext, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { useOptionalConsumer } from '@/auth/AuthProvider'
import { CopyableId } from '@/components/ux/CopyableId'

const ActorNames = createContext<Record<string, string>>({})

export function RunActorNames({ threadId, revision, enabled, children }: {
  threadId: string; revision?: string | null; enabled: boolean; children: ReactNode
}) {
  const query = useQuery({
    queryKey: ['pipelines', threadId, 'actors', revision], enabled, retry: false,
    queryFn: async ({ signal }) => {
      const { data, error, response } = await getApexClient().GET('/v1/pipelines/{thread_id}/actors', { params: { path: { thread_id: threadId } }, signal })
      if (!response.ok || !data) throw new ApiError(response.status, errorMessageOf(error, 'Actor names are unavailable'), error)
      return data
    },
  })
  return <ActorNames.Provider value={query.isError ? {} : query.data?.names ?? {}}>{children}{query.data?.truncated && <p className="ux-context">Some actor names were omitted from this bounded lookup. Recorded actor IDs remain available.</p>}</ActorNames.Provider>
}

/** Actors are authenticated principals, never application identifiers. */
export function RunActorIdentity({ id }: { id?: string | null }) {
  const names = useContext(ActorNames)
  const consumer = useOptionalConsumer()
  if (!id || id === 'unknown') return <span>Actor was not recorded</span>
  const name = names[id] ?? (consumer?.id === id ? consumer.name : undefined)
  return <span>{name ?? 'Name unavailable'}{' '}<CopyableId value={id} label="Actor ID" /></span>
}
