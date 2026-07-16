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
  type QueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { fetchAllOffsetPages } from '@/api/fetchAllPages'
import { queryKeys, STALE_TIMES } from '@/api/queryKeys'
import {
  acceptConsumerKeyOperation,
  beginConsumerKeyOperation,
  rejectConsumerKeyOperation,
  type ConsumerKeyOperationToken,
} from '@/auth/consumerKeyHandoff'

export type Consumer = components['schemas']['ConsumerRead']
export type ConsumerCreated = components['schemas']['ConsumerCreated']
export type ConsumerCreateRequest = components['schemas']['ConsumerCreateRequest']
export type ConsumerUpdateRequest = components['schemas']['ConsumerUpdateRequest']
export type ConsumerType = components['schemas']['ConsumerType']
export type ScopeRef = components['schemas']['ScopeRef']
export type CurrentPrincipal = components['schemas']['CurrentPrincipalResponse']

export const CONSUMER_TYPES: readonly ConsumerType[] = ['dashboard', 'headless', 'internal']
const CONSUMERS_PAGE_SIZE = 200
const deletedConsumerIds = new WeakMap<QueryClient, Set<string>>()

export function consumerWriteMutationKey(consumerId: string) {
  return ['admin', 'consumers', 'write', consumerId] as const
}

function deletedIdsFor(queryClient: QueryClient): Set<string> {
  const existing = deletedConsumerIds.get(queryClient)
  if (existing) return existing
  const created = new Set<string>()
  deletedConsumerIds.set(queryClient, created)
  return created
}

function isConsumerDeleted(queryClient: QueryClient, consumerId: string): boolean {
  return deletedConsumerIds.get(queryClient)?.has(consumerId) ?? false
}

/** Strips the one-time api_key so only ConsumerRead fields reach the cache. */
function toConsumerRead(created: ConsumerCreated): Consumer {
  const read: Consumer & { api_key?: string } = { ...created }
  delete read.api_key
  return read
}

async function fetchConsumers(signal?: AbortSignal): Promise<Consumer[]> {
  return fetchAllOffsetPages({
    label: 'Consumers',
    pageSize: CONSUMERS_PAGE_SIZE,
    fetchPage: async (limit, offset) => {
      const { data, response } = await getApexClient().GET('/v1/admin/consumers', {
        params: { query: { limit, offset } },
        signal,
      })
      if (!response.ok || !data) {
        throw new ApiError(response.status, `Consumers request failed (${response.status})`)
      }
      return data
    },
  })
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
    queryFn: ({ signal }) => fetchConsumers(signal),
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
  ConsumerCreateRequest,
  ConsumerKeyOperationToken
> {
  const queryClient = useQueryClient()
  return useMutation({
    gcTime: 0,
    onMutate: (body) =>
      beginConsumerKeyOperation({
        kind: 'created',
        consumerName: body.name,
      }),
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
    onSuccess: (created, _body, token) => {
      acceptConsumerKeyOperation(token, created)
      deletedIdsFor(queryClient).delete(created.id)
      queryClient.setQueryData(queryKeys.admin.consumer(created.id), toConsumerRead(created))
      void queryClient.invalidateQueries({ queryKey: queryKeys.admin.consumers() })
    },
    onError: (_error, _body, token) => {
      rejectConsumerKeyOperation(token)
    },
  })
}

export interface UpdateConsumerInput {
  consumerId: string
  body: ConsumerUpdateRequest
}

export function useUpdateConsumer(
  consumerId: string,
): UseMutationResult<Consumer, Error, UpdateConsumerInput> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: consumerWriteMutationKey(consumerId),
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
      if (isConsumerDeleted(queryClient, updated.id)) return
      queryClient.setQueryData(queryKeys.admin.consumer(updated.id), updated)
      void queryClient.invalidateQueries({ queryKey: queryKeys.admin.consumers() })
    },
  })
}

/** 409 = self-delete; the page maps it to "You cannot delete your own consumer". */
export function useDeleteConsumer(consumerId: string): UseMutationResult<void, Error, string> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: consumerWriteMutationKey(consumerId),
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
      deletedIdsFor(queryClient).add(consumerId)
      queryClient.removeQueries({ queryKey: queryKeys.admin.consumer(consumerId) })
      void queryClient.invalidateQueries({ queryKey: queryKeys.admin.consumers() })
    },
  })
}

export interface RotateConsumerKeyInput {
  consumerId: string
  consumerName: string
  isCurrentConsumer: boolean
}

/** Same one-time api_key contract as create, with time to safely hand off clients. */
export function useRotateConsumerKey(consumerId: string): UseMutationResult<
  ConsumerCreated,
  Error,
  RotateConsumerKeyInput,
  ConsumerKeyOperationToken
> {
  const queryClient = useQueryClient()
  return useMutation({
    gcTime: 0,
    mutationKey: consumerWriteMutationKey(consumerId),
    onMutate: ({ consumerName, isCurrentConsumer }) =>
      beginConsumerKeyOperation({
        kind: 'rotated',
        consumerName,
        isCurrentConsumer,
      }),
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
    onSuccess: (rotated, _variables, token) => {
      if (isConsumerDeleted(queryClient, rotated.id)) {
        rejectConsumerKeyOperation(token)
        return
      }
      acceptConsumerKeyOperation(token, rotated)
      queryClient.setQueryData(queryKeys.admin.consumer(rotated.id), toConsumerRead(rotated))
      void queryClient.invalidateQueries({ queryKey: queryKeys.admin.consumers() })
    },
    onError: (_error, _variables, token) => {
      rejectConsumerKeyOperation(token)
    },
  })
}
