import type { components } from '@apex/api-client'

import {
  getApiKeyRevision,
  getSessionRevision,
  subscribeApiKey,
  subscribeSession,
} from '@/auth/keyStorage'

type ConsumerCreated = components['schemas']['ConsumerCreated']

export type ConsumerKeyOperationKind = 'created' | 'rotated'

export interface PendingConsumerKeyOperation {
  id: number
  kind: ConsumerKeyOperationKind
  consumerName: string
}

export interface ConsumerKeyHandoff {
  id: number
  kind: ConsumerKeyOperationKind
  created: ConsumerCreated
  isCurrentConsumer: boolean
}

export interface ConsumerKeyHandoffSnapshot {
  pending: readonly PendingConsumerKeyOperation[]
  handoffs: readonly ConsumerKeyHandoff[]
}

export interface ConsumerKeyOperationToken {
  id: number
  keyRevision: number
  sessionRevision: number
  lifecycleRevision: number
}

interface BeginConsumerKeyOperationInput {
  kind: ConsumerKeyOperationKind
  consumerName: string
  isCurrentConsumer?: boolean
}

interface PendingEntry extends PendingConsumerKeyOperation {
  isCurrentConsumer: boolean
}

const listeners = new Set<() => void>()
const pending = new Map<number, PendingEntry>()
let handoffs: ConsumerKeyHandoff[] = []
let operationSequence = 0
let lifecycleRevision = 0
let snapshot: ConsumerKeyHandoffSnapshot = { pending: [], handoffs: [] }

function publish(): void {
  snapshot = {
    pending: Array.from(pending.values(), ({ id, kind, consumerName }) => ({
      id,
      kind,
      consumerName,
    })),
    handoffs: [...handoffs],
  }
  for (const listener of listeners) listener()
}

function belongsToCurrentLifecycle(token: ConsumerKeyOperationToken): boolean {
  return (
    token.lifecycleRevision === lifecycleRevision &&
    token.keyRevision === getApiKeyRevision() &&
    token.sessionRevision === getSessionRevision()
  )
}

/**
 * Starts an auth-scoped one-time-key operation before the request is issued. The token is
 * bound to both authentication revisions and the current mounted app
 * lifecycle, so a late response can never surface in another session.
 */
export function beginConsumerKeyOperation(
  input: BeginConsumerKeyOperationInput,
): ConsumerKeyOperationToken {
  const id = ++operationSequence
  pending.set(id, {
    id,
    kind: input.kind,
    consumerName: input.consumerName,
    isCurrentConsumer: input.isCurrentConsumer ?? false,
  })
  publish()
  return {
    id,
    keyRevision: getApiKeyRevision(),
    sessionRevision: getSessionRevision(),
    lifecycleRevision,
  }
}

/** Accepts the exactly-once secret into an auth-scoped in-memory reveal queue. */
export function acceptConsumerKeyOperation(
  token: ConsumerKeyOperationToken | undefined,
  created: ConsumerCreated,
): void {
  if (!token || !belongsToCurrentLifecycle(token)) return
  const operation = pending.get(token.id)
  if (!operation) return
  pending.delete(token.id)
  handoffs = [
    ...handoffs,
    {
      id: operation.id,
      kind: operation.kind,
      created,
      isCurrentConsumer: operation.isCurrentConsumer,
    },
  ]
  publish()
}

/** Clears a failed operation without creating a reveal entry. */
export function rejectConsumerKeyOperation(token: ConsumerKeyOperationToken | undefined): void {
  if (!token || token.lifecycleRevision !== lifecycleRevision) return
  if (!pending.delete(token.id)) return
  publish()
}

/** Removes a secret only after the operator explicitly acknowledges it. */
export function acknowledgeConsumerKeyHandoff(id: number): void {
  const next = handoffs.filter((entry) => entry.id !== id)
  if (next.length === handoffs.length) return
  handoffs = next
  publish()
}

export function getConsumerKeyHandoffSnapshot(): ConsumerKeyHandoffSnapshot {
  return snapshot
}

export function subscribeConsumerKeyHandoff(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/**
 * Invalidates pending callbacks and clears secrets when the app lifecycle or
 * authenticated principal changes. AppShell calls this on unmount; the module
 * subscriptions cover key changes and semantic session revisions.
 */
export function invalidateConsumerKeyHandoffLifecycle(): void {
  lifecycleRevision += 1
  pending.clear()
  handoffs = []
  publish()
}

subscribeApiKey(invalidateConsumerKeyHandoffLifecycle)
subscribeSession(invalidateConsumerKeyHandoffLifecycle)
