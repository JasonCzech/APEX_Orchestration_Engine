import { createContext, useContext, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { useOptionalConsumer } from '@/auth/AuthProvider'
import { CopyableId } from '@/components/ux/CopyableId'

const Names = createContext<Record<string, string>>({})
export function CycleActorNames({ cycleId, revision, children }: { cycleId: string; revision: number; children: ReactNode }) {
  const query = useQuery({ queryKey: ['cycles', cycleId, 'actors', revision], retry: false,
    queryFn: async ({ signal }) => {
      const { data, error, response } = await getApexClient().GET('/v1/cycles/{cycle_id}/actors', { params: { path: { cycle_id: cycleId } }, signal })
      if (!response.ok || !data) throw new ApiError(response.status, errorMessageOf(error, 'Actor names are unavailable'), error)
      return data
    },
  })
  return <Names.Provider value={query.isError ? {} : query.data?.names ?? {}}>{children}{query.data?.truncated && <p className="tonal-card info">Some actor names were omitted from this bounded lookup. Full recorded IDs remain available.</p>}</Names.Provider>
}

export function CycleActorIdentity({ id, name }: { id?: string | null; name?: string | null }) {
  const names = useContext(Names)
  const consumer = useOptionalConsumer()
  if (!id) return <>Identity not recorded</>
  const resolved = names[id] ?? name ?? (consumer?.id === id ? consumer.name : undefined)
  return <span>{resolved ?? 'Name unavailable'}{' '}<CopyableId value={id} label="Actor ID" /></span>
}
