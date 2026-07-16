/**
 * Durable, principal-scoped idempotency keys for browser mutations whose
 * response can be ambiguous (for example, a timeout after the server commits).
 *
 * Keys are indexed by the canonical request payload rather than component
 * lifecycle. That makes an edit create a distinct attempt while edit/revert,
 * modal reopen, and component remount recover the original attempt. Records
 * left by a prior page runtime are quarantined because the new page cannot
 * distinguish an unresolved request from a completed request whose cleanup
 * failed.
 */

import {
  getApiKeyRevision,
  getSessionRevision,
  subscribeApiKey,
  subscribeSession,
} from '@/auth/keyStorage'

export const PIPELINE_LAUNCH_IDEMPOTENCY_STORAGE_KEY =
  'apex.idempotency.pipeline-launch.v1'
export const PIPELINE_RERUN_IDEMPOTENCY_STORAGE_KEY =
  'apex.idempotency.pipeline-rerun.v1'
export const SAFE_RETRY_STORAGE_ERROR =
  'Safe retry storage is unavailable; enable session storage and try again.'
export const SAFE_RETRY_RECOVERY_ERROR =
  'A prior request from another page lifecycle has an unresolved result; change the request or clear this tab’s site data before retrying.'

interface StoredIdempotencyKey {
  idempotencyKey: string
  runtimeId: string | null
  keyRevision: number | null
  sessionRevision: number | null
}

type StoredIdempotencyBlock = Pick<
  StoredIdempotencyKey,
  'runtimeId' | 'keyRevision' | 'sessionRevision'
>

interface StoredIdempotencyKeys {
  version: 3
  keysByPayload: Record<string, StoredIdempotencyKey>
  blockedPayloads: Record<string, StoredIdempotencyBlock>
}

interface StoredIdempotencyAttempt {
  idempotencyKey: string
  requestPayload: unknown
  runtimeId: string | null
  keyRevision: number | null
  sessionRevision: number | null
}

interface StoredIdempotencyAttempts {
  version: 3
  attemptsByPayload: Record<string, StoredIdempotencyAttempt>
  blockedPayloads: Record<string, StoredIdempotencyBlock>
}

export interface DurableIdempotencyAttempt<T> {
  idempotencyKey: string
  requestPayload: T
}

const attemptCreations = new Map<
  string,
  Promise<DurableIdempotencyAttempt<unknown>>
>()
const retiredKeys = new Map<string, string>()
const retiredAttempts = new Map<string, string>()
const SAFE_RETRY_RUNTIME_ID =
  typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `runtime-${Date.now()}-${Math.random().toString(36).slice(2)}`

type StoredRecordDisposition = 'current' | 'replaced-session' | 'prior-runtime'

function recordDisposition(
  record: Pick<
    StoredIdempotencyKey,
    'runtimeId' | 'keyRevision' | 'sessionRevision'
  >,
  keyRevision: number,
): StoredRecordDisposition {
  if (record.runtimeId !== SAFE_RETRY_RUNTIME_ID) return 'prior-runtime'
  // A semantic-session revision rejects stale network responses, but an
  // unchanged API key is still the same server idempotency principal.
  return record.keyRevision === keyRevision ? 'current' : 'replaced-session'
}

function retirementIdentity(
  storageKey: string,
  signature: string,
  keyRevision: number,
): string {
  return JSON.stringify([storageKey, signature, keyRevision])
}

function clearCredentialState(): void {
  attemptCreations.clear()
  retiredKeys.clear()
  retiredAttempts.clear()
}

subscribeApiKey(clearCredentialState)
subscribeSession(() => {
  // Resolvers started under the prior authorization snapshot must not be
  // joined, while completed-key tombstones remain valid for this API key.
  attemptCreations.clear()
})

function storage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.sessionStorage
  } catch {
    return null
  }
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize)
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
        .map(([key, item]) => [key, canonicalize(item)]),
    )
  }
  return value
}

export function stableIdempotencyPayload(value: unknown): string {
  return JSON.stringify(canonicalize(value)) ?? 'undefined'
}

export async function idempotencyPayloadSignature(value: unknown): Promise<string> {
  const subtle = globalThis.crypto?.subtle
  if (!subtle) {
    throw new Error('Secure browser hashing is unavailable; cannot create an idempotent request.')
  }
  const bytes = new TextEncoder().encode(stableIdempotencyPayload(value))
  const digest = await subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

function emptyKeys(): StoredIdempotencyKeys {
  return { version: 3, keysByPayload: {}, blockedPayloads: {} }
}

function validIdempotencyKey(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 128
}

function storedMetadata(value: {
  runtimeId?: unknown
  keyRevision?: unknown
  sessionRevision?: unknown
}): StoredIdempotencyBlock {
  return {
    runtimeId: typeof value.runtimeId === 'string' ? value.runtimeId : null,
    keyRevision: Number.isSafeInteger(value.keyRevision) ? Number(value.keyRevision) : null,
    sessionRevision: Number.isSafeInteger(value.sessionRevision)
      ? Number(value.sessionRevision)
      : null,
  }
}

function readBlockedPayloads(
  value: unknown,
): Record<string, StoredIdempotencyBlock> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  return Object.fromEntries(
    Object.entries(value).flatMap(([signature, record]) => {
      if (
        !/^[a-f0-9]{64}$/.test(signature) ||
        !record ||
        typeof record !== 'object' ||
        Array.isArray(record)
      ) {
        return []
      }
      return [[signature, storedMetadata(record)] as const]
    }),
  )
}

function readKeys(storageKey: string): StoredIdempotencyKeys {
  try {
    const serialized = storage()?.getItem(storageKey)
    if (!serialized) return emptyKeys()
    const parsed = JSON.parse(serialized) as {
      version?: unknown
      keysByPayload?: unknown
      blockedPayloads?: unknown
    }
    const supportedVersion =
      parsed.version === 1 || parsed.version === 2 || parsed.version === 3
    if (
      !supportedVersion ||
      !parsed.keysByPayload ||
      typeof parsed.keysByPayload !== 'object' ||
      Array.isArray(parsed.keysByPayload)
    ) {
      return emptyKeys()
    }
    const legacy = parsed.version === 1
    return {
      version: 3,
      keysByPayload: Object.fromEntries(
        Object.entries(parsed.keysByPayload).flatMap(([signature, value]) => {
          if (!/^[a-f0-9]{64}$/.test(signature)) return []
          if (legacy && validIdempotencyKey(value)) {
            return [
              [
                signature,
                {
                  idempotencyKey: value,
                  runtimeId: null,
                  keyRevision: null,
                  sessionRevision: null,
                },
              ] as const,
            ]
          }
          if (!value || typeof value !== 'object' || Array.isArray(value)) return []
          const record = value as {
            idempotencyKey?: unknown
            runtimeId?: unknown
            keyRevision?: unknown
            sessionRevision?: unknown
          }
          if (!validIdempotencyKey(record.idempotencyKey)) return []
          return [
            [
              signature,
              {
                idempotencyKey: record.idempotencyKey,
                ...storedMetadata(record),
              },
            ] as const,
          ]
        }),
      ),
      blockedPayloads:
        parsed.version === 3
          ? readBlockedPayloads(parsed.blockedPayloads)
          : {},
    }
  } catch {
    return emptyKeys()
  }
}

function writeKeys(storageKey: string, state: StoredIdempotencyKeys): boolean {
  try {
    const target = storage()
    if (!target) return false
    const serialized = JSON.stringify(state)
    target.setItem(storageKey, serialized)
    return target.getItem(storageKey) === serialized
  } catch {
    return false
  }
}

function emptyAttempts(): StoredIdempotencyAttempts {
  return { version: 3, attemptsByPayload: {}, blockedPayloads: {} }
}

function readAttempts(storageKey: string): StoredIdempotencyAttempts {
  try {
    const serialized = storage()?.getItem(storageKey)
    if (!serialized) return emptyAttempts()
    const parsed = JSON.parse(serialized) as {
      version?: unknown
      attemptsByPayload?: unknown
      blockedPayloads?: unknown
    }
    const supportedVersion =
      parsed.version === 1 || parsed.version === 2 || parsed.version === 3
    if (
      !supportedVersion ||
      !parsed.attemptsByPayload ||
      typeof parsed.attemptsByPayload !== 'object' ||
      Array.isArray(parsed.attemptsByPayload)
    ) {
      return emptyAttempts()
    }
    return {
      version: 3,
      attemptsByPayload: Object.fromEntries(
        Object.entries(parsed.attemptsByPayload).flatMap(([signature, value]) => {
          if (
            !/^[a-f0-9]{64}$/.test(signature) ||
            !value ||
            typeof value !== 'object' ||
            Array.isArray(value)
          ) {
            return []
          }
          const attempt = value as {
            idempotencyKey?: unknown
            requestPayload?: unknown
            runtimeId?: unknown
            keyRevision?: unknown
            sessionRevision?: unknown
          }
          if (!validIdempotencyKey(attempt.idempotencyKey)) return []
          return [
            [
              signature,
              {
                idempotencyKey: attempt.idempotencyKey,
                requestPayload: attempt.requestPayload,
                ...storedMetadata(attempt),
              },
            ] as const,
          ]
        }),
      ),
      blockedPayloads:
        parsed.version === 3
          ? readBlockedPayloads(parsed.blockedPayloads)
          : {},
    }
  } catch {
    return emptyAttempts()
  }
}

function writeAttempts(storageKey: string, state: StoredIdempotencyAttempts): boolean {
  try {
    const target = storage()
    if (!target) return false
    const serialized = JSON.stringify(state)
    target.setItem(storageKey, serialized)
    return target.getItem(storageKey) === serialized
  } catch {
    return false
  }
}

function revisionsAreCurrent(keyRevision: number, sessionRevision: number): boolean {
  return keyRevision === getApiKeyRevision() && sessionRevision === getSessionRevision()
}

function currentKeysOnly(
  state: StoredIdempotencyKeys,
  keyRevision: number,
): Record<string, StoredIdempotencyKey> {
  return Object.fromEntries(
    Object.entries(state.keysByPayload).filter(
      ([, record]) => recordDisposition(record, keyRevision) === 'current',
    ),
  )
}

function currentAttemptsOnly(
  state: StoredIdempotencyAttempts,
  keyRevision: number,
): Record<string, StoredIdempotencyAttempt> {
  return Object.fromEntries(
    Object.entries(state.attemptsByPayload).filter(
      ([, record]) => recordDisposition(record, keyRevision) === 'current',
    ),
  )
}

function activeBlockedPayloads(
  blockedPayloads: Record<string, StoredIdempotencyBlock>,
  records: Record<string, StoredIdempotencyBlock>,
  keyRevision: number,
  sessionRevision: number,
): Record<string, StoredIdempotencyBlock> {
  const currentMetadata: StoredIdempotencyBlock = {
    runtimeId: SAFE_RETRY_RUNTIME_ID,
    keyRevision,
    sessionRevision,
  }
  const active = Object.fromEntries(
    Object.entries(blockedPayloads).flatMap(([signature, record]) =>
      recordDisposition(record, keyRevision) === 'replaced-session'
        ? []
        : [[signature, currentMetadata] as const],
    ),
  )
  for (const [signature, record] of Object.entries(records)) {
    if (recordDisposition(record, keyRevision) === 'prior-runtime') {
      active[signature] = currentMetadata
    }
  }
  return active
}

function blockedInCurrentLifecycle(
  block: StoredIdempotencyBlock | undefined,
  keyRevision: number,
): boolean {
  return (
    block !== undefined &&
    recordDisposition(block, keyRevision) !== 'replaced-session'
  )
}

export async function getDurableIdempotencyKey(
  storageKey: string,
  payload: unknown,
  createKey: () => string,
): Promise<string> {
  const keyRevision = getApiKeyRevision()
  const sessionRevision = getSessionRevision()
  const signature = await idempotencyPayloadSignature(payload)
  if (keyRevision !== getApiKeyRevision() || sessionRevision !== getSessionRevision()) {
    throw new Error('Credentials changed while preparing the request; please retry.')
  }
  const retirementKey = retirementIdentity(
    storageKey,
    signature,
    keyRevision,
  )
  const retiredKey = retiredKeys.get(retirementKey)
  const state = readKeys(storageKey)
  if (
    blockedInCurrentLifecycle(
      state.blockedPayloads[signature],
      keyRevision,
    )
  ) {
    throw new Error(SAFE_RETRY_RECOVERY_ERROR)
  }
  const existing = state.keysByPayload[signature]
  if (existing) {
    const disposition = recordDisposition(existing, keyRevision)
    if (disposition === 'prior-runtime') throw new Error(SAFE_RETRY_RECOVERY_ERROR)
    if (
      disposition === 'current' &&
      existing.idempotencyKey !== retiredKey
    ) {
      retiredKeys.delete(retirementKey)
      return existing.idempotencyKey
    }
  }

  const keysByPayload = currentKeysOnly(state, keyRevision)
  const blockedPayloads = activeBlockedPayloads(
    state.blockedPayloads,
    state.keysByPayload,
    keyRevision,
    sessionRevision,
  )
  const key = createKey()
  keysByPayload[signature] = {
    idempotencyKey: key,
    runtimeId: SAFE_RETRY_RUNTIME_ID,
    keyRevision,
    sessionRevision,
  }
  if (
    !writeKeys(storageKey, {
      version: 3,
      keysByPayload,
      blockedPayloads,
    })
  ) {
    throw new Error(SAFE_RETRY_STORAGE_ERROR)
  }
  retiredKeys.delete(retirementKey)
  return key
}

/**
 * Bind an idempotency key to the exact resolved request payload it protects.
 *
 * Some browser intents contain mutable references (for example a work item
 * key whose description is resolved just before launch). A retry must replay
 * the original resolved body as well as the original key, otherwise the
 * server correctly rejects the key as being reused for a different request.
 */
export async function getDurableIdempotencyAttempt<T>({
  storageKey,
  intent,
  createKey,
  createRequestPayload,
  validateRequestPayload,
}: {
  storageKey: string
  intent: unknown
  createKey: () => string
  createRequestPayload: () => Promise<T>
  validateRequestPayload: (value: unknown) => value is T
}): Promise<DurableIdempotencyAttempt<T>> {
  const keyRevision = getApiKeyRevision()
  const sessionRevision = getSessionRevision()
  const signature = await idempotencyPayloadSignature(intent)
  if (!revisionsAreCurrent(keyRevision, sessionRevision)) {
    throw new Error('Credentials changed while preparing the request; please retry.')
  }
  const retirementKey = retirementIdentity(
    storageKey,
    signature,
    keyRevision,
  )

  const restore = (
    attempt: StoredIdempotencyAttempt | undefined,
  ): DurableIdempotencyAttempt<T> | null => {
    if (!attempt) return null
    const disposition = recordDisposition(attempt, keyRevision)
    if (disposition === 'replaced-session') return null
    if (disposition === 'prior-runtime') throw new Error(SAFE_RETRY_RECOVERY_ERROR)
    const retiredKey = retiredAttempts.get(retirementKey)
    if (attempt.idempotencyKey === retiredKey) return null
    if (retiredKey) retiredAttempts.delete(retirementKey)
    if (!validateRequestPayload(attempt.requestPayload)) {
      throw new Error('Saved retry data is invalid; change the request and try again.')
    }
    return {
      idempotencyKey: attempt.idempotencyKey,
      requestPayload: attempt.requestPayload,
    }
  }

  const restoreFromState = (
    state: StoredIdempotencyAttempts,
  ): DurableIdempotencyAttempt<T> | null => {
    if (
      blockedInCurrentLifecycle(
        state.blockedPayloads[signature],
        keyRevision,
      )
    ) {
      throw new Error(SAFE_RETRY_RECOVERY_ERROR)
    }
    return restore(state.attemptsByPayload[signature])
  }

  const restored = restoreFromState(readAttempts(storageKey))
  if (restored) return restored

  // A replacement principal/session must never join a resolver that started
  // under the prior identity, even when the browser intent is identical.
  const creationKey = JSON.stringify([
    storageKey,
    signature,
    SAFE_RETRY_RUNTIME_ID,
    keyRevision,
    sessionRevision,
  ])
  const pending = attemptCreations.get(creationKey)
  if (pending) {
    const attempt = await pending
    if (!revisionsAreCurrent(keyRevision, sessionRevision)) {
      throw new Error('Credentials changed while preparing the request; please retry.')
    }
    if (!validateRequestPayload(attempt.requestPayload)) {
      throw new Error('Saved retry data is invalid; change the request and try again.')
    }
    return attempt as DurableIdempotencyAttempt<T>
  }

  const creation = (async (): Promise<DurableIdempotencyAttempt<T>> => {
    const raced = restoreFromState(readAttempts(storageKey))
    if (raced) return raced

    const requestPayload = await createRequestPayload()
    if (!revisionsAreCurrent(keyRevision, sessionRevision)) {
      throw new Error('Credentials changed while preparing the request; please retry.')
    }
    if (!validateRequestPayload(requestPayload)) {
      throw new Error('Resolved request data is invalid; change the request and try again.')
    }

    const state = readAttempts(storageKey)
    const afterResolve = restoreFromState(state)
    if (afterResolve) return afterResolve

    const attempt: DurableIdempotencyAttempt<T> = {
      idempotencyKey: createKey(),
      requestPayload,
    }
    const attemptsByPayload = {
      ...currentAttemptsOnly(state, keyRevision),
      [signature]: {
        ...attempt,
        runtimeId: SAFE_RETRY_RUNTIME_ID,
        keyRevision,
        sessionRevision,
      },
    }
    const blockedPayloads = activeBlockedPayloads(
      state.blockedPayloads,
      state.attemptsByPayload,
      keyRevision,
      sessionRevision,
    )
    if (
      !writeAttempts(storageKey, {
        version: 3,
        attemptsByPayload,
        blockedPayloads,
      })
    ) {
      throw new Error(SAFE_RETRY_STORAGE_ERROR)
    }
    retiredAttempts.delete(retirementKey)
    return attempt
  })()

  attemptCreations.set(
    creationKey,
    creation as Promise<DurableIdempotencyAttempt<unknown>>,
  )
  try {
    return await creation
  } finally {
    if (attemptCreations.get(creationKey) === creation) {
      attemptCreations.delete(creationKey)
    }
  }
}

/**
 * Retire only the exact key that received a confirmed success. The equality
 * guard prevents an older in-flight response from deleting a replacement
 * principal's mapping after session-bound storage has been purged.
 */
export async function retireDurableIdempotencyKey(
  storageKey: string,
  payload: unknown,
  confirmedKey: string,
): Promise<void> {
  const keyRevision = getApiKeyRevision()
  const sessionRevision = getSessionRevision()
  const signature = await idempotencyPayloadSignature(payload)
  if (keyRevision !== getApiKeyRevision() || sessionRevision !== getSessionRevision()) return
  const retirementKey = retirementIdentity(
    storageKey,
    signature,
    keyRevision,
  )
  retiredKeys.set(retirementKey, confirmedKey)
  const state = readKeys(storageKey)
  const existing = state.keysByPayload[signature]
  if (
    existing?.idempotencyKey !== confirmedKey ||
    recordDisposition(existing, keyRevision) !== 'current'
  ) {
    return
  }
  const keysByPayload = currentKeysOnly(state, keyRevision)
  delete keysByPayload[signature]
  const blockedPayloads = activeBlockedPayloads(
    state.blockedPayloads,
    state.keysByPayload,
    keyRevision,
    sessionRevision,
  )
  if (
    writeKeys(storageKey, {
      version: 3,
      keysByPayload,
      blockedPayloads,
    })
  ) {
    retiredKeys.delete(retirementKey)
  }
}

/** Retire the exact resolved attempt after the server confirms success. */
export async function retireDurableIdempotencyAttempt(
  storageKey: string,
  intent: unknown,
  confirmedKey: string,
): Promise<void> {
  const keyRevision = getApiKeyRevision()
  const sessionRevision = getSessionRevision()
  const signature = await idempotencyPayloadSignature(intent)
  if (!revisionsAreCurrent(keyRevision, sessionRevision)) return
  const retirementKey = retirementIdentity(
    storageKey,
    signature,
    keyRevision,
  )
  retiredAttempts.set(retirementKey, confirmedKey)
  const state = readAttempts(storageKey)
  const existing = state.attemptsByPayload[signature]
  if (
    existing?.idempotencyKey !== confirmedKey ||
    recordDisposition(existing, keyRevision) !== 'current'
  ) {
    return
  }
  const attemptsByPayload = currentAttemptsOnly(state, keyRevision)
  delete attemptsByPayload[signature]
  const blockedPayloads = activeBlockedPayloads(
    state.blockedPayloads,
    state.attemptsByPayload,
    keyRevision,
    sessionRevision,
  )
  if (
    writeAttempts(storageKey, {
      version: 3,
      attemptsByPayload,
      blockedPayloads,
    })
  ) {
    retiredAttempts.delete(retirementKey)
  }
}
