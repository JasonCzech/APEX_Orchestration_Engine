/**
 * API consumer management over /v1/admin/consumers (plan route
 * /admin/consumers, D7). Admin-role only.
 *
 * Create and rotate answer with `api_key` EXACTLY ONCE (the server stores only
 * a sha256 hash) — these hooks hand the payload to the caller for the
 * key-reveal modal and never write it into the query cache: the cache is
 * seeded with the ConsumerRead fields only.
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys, STALE_TIMES } from '@/api/queryKeys'

export type Consumer = components['schemas']['ConsumerRead']
export type ConsumerCreated = components['schemas']['ConsumerCreated']
export type ConsumerCreateRequest = components['schemas']['ConsumerCreateRequest']
export type ConsumerUpdateRequest = components['schemas']['ConsumerUpdateRequest']
export type ConsumerType = components['schemas']['ConsumerType']
export type ScopeRef = components['schemas']['ScopeRef']
export type CurrentPrincipal = components['schemas']['CurrentPrincipalResponse']

export const CONSUMER_TYPES: readonly ConsumerType[] = ['dashboard', 'headless', 'internal']

/** Strips the one-time api_key so only ConsumerRead fields reach the cache. */
function toConsumerRead(created: ConsumerCreated): Consumer {
  const read: Consumer & { api_key?: string } = { ...created }
  delete read.api_key
  return read
}

async function fetchConsumers(): Promise<Consumer[]> {
  const { data, response } = await getApexClient().GET('/v1/admin/consumers', {})
  if (!response.ok || !data) {
    throw new ApiError(response.status, `Consumers request failed (${response.status})`)
  }
  return data
}

async function fetchConsumer(consumerId: string): Promise<Consumer> {
  const { data, error, response } = await getApexClient().GET(
    '/v1/admin/consumers/{consumer_id}',
    { params: { path: { consumer_id: consumerId } } },
  )
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Consumer request failed (${response.status})`),
      error,
    )
  }
  return data
}

export function useConsumersIndex(): UseQueryResult<Consumer[], Error> {
  return useQuery({
    queryKey: queryKeys.admin.consumers(),
    queryFn: fetchConsumers,
    staleTime: STALE_TIMES.admin,
  })
}

/** Authenticated principal id is needed to make self-key rotation safe. */
export function useCurrentPrincipal(): UseQueryResult<CurrentPrincipal, Error> {
  return useQuery({
    queryKey: queryKeys.system.principal(),
    queryFn: async () => {
      const { data, response } = await getApexClient().GET('/v1/auth/me')
      if (!response.ok || !data) {
        throw new ApiError(response.status, `Current principal request failed (${response.status})`)
      }
      return data
    },
    staleTime: STALE_TIMES.admin,
  })
}

export function useConsumerDetail(consumerId: string | undefined): UseQueryResult<Consumer, Error> {
  return useQuery({
    queryKey: queryKeys.admin.consumer(consumerId ?? ''),
    queryFn: () => fetchConsumer(consumerId ?? ''),
    enabled: Boolean(consumerId),
    staleTime: STALE_TIMES.admin,
  })
}

/** Response carries the raw api_key exactly once — show it, never cache it. */
export function useCreateConsumer(): UseMutationResult<
  ConsumerCreated,
  Error,
  ConsumerCreateRequest
> {
  const queryClient = useQueryClient()
  return useMutation({
    gcTime: 0,
    mutationFn: async (body: ConsumerCreateRequest) => {
      const { data, error, response } = await getApexClient().POST('/v1/admin/consumers', {
        body,
      })
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Consumer create failed (${response.status})`),
          error,
        )
      }
      return data
    },
    onSuccess: (created) => {
      queryClient.setQueryData(queryKeys.admin.consumer(created.id), toConsumerRead(created))
      void queryClient.invalidateQueries({ queryKey: queryKeys.admin.consumers() })
    },
  })
}

export interface UpdateConsumerInput {
  consumerId: string
  body: ConsumerUpdateRequest
}

export function useUpdateConsumer(): UseMutationResult<Consumer, Error, UpdateConsumerInput> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ consumerId, body }: UpdateConsumerInput) => {
      const { data, error, response } = await getApexClient().PATCH(
        '/v1/admin/consumers/{consumer_id}',
        { params: { path: { consumer_id: consumerId } }, body },
      )
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Consumer update failed (${response.status})`),
          error,
        )
      }
      return data
    },
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.admin.consumer(updated.id), updated)
      void queryClient.invalidateQueries({ queryKey: queryKeys.admin.consumers() })
    },
  })
}

/** 409 = self-delete; the page maps it to "You cannot delete your own consumer". */
export function useDeleteConsumer(): UseMutationResult<void, Error, string> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (consumerId: string) => {
      const { error, response } = await getApexClient().DELETE(
        '/v1/admin/consumers/{consumer_id}',
        { params: { path: { consumer_id: consumerId } } },
      )
      if (!response.ok) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Consumer delete failed (${response.status})`),
          error,
        )
      }
    },
    onSuccess: (_void, consumerId) => {
      queryClient.removeQueries({ queryKey: queryKeys.admin.consumer(consumerId) })
      void queryClient.invalidateQueries({ queryKey: queryKeys.admin.consumers() })
    },
  })
}

export interface RotateConsumerKeyInput {
  consumerId: string
}

/** Same one-time api_key contract as create, with time to safely hand off clients. */
export function useRotateConsumerKey(): UseMutationResult<
  ConsumerCreated,
  Error,
  RotateConsumerKeyInput
> {
  const queryClient = useQueryClient()
  return useMutation({
    gcTime: 0,
    mutationFn: async ({ consumerId }: RotateConsumerKeyInput) => {
      const { data, error, response } = await getApexClient().POST(
        '/v1/admin/consumers/{consumer_id}/rotate',
        {
          params: { path: { consumer_id: consumerId } },
          body: { grace_period_seconds: 5 * 60 },
        },
      )
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Key rotation failed (${response.status})`),
          error,
        )
      }
      return data
    },
    onSuccess: (rotated) => {
      queryClient.setQueryData(queryKeys.admin.consumer(rotated.id), toConsumerRead(rotated))
      void queryClient.invalidateQueries({ queryKey: queryKeys.admin.consumers() })
    },
  })
}
