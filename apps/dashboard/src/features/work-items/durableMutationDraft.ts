import {
  getApiKeyRevision,
  getSessionRevision,
  subscribeApiKey,
} from '@/auth/keyStorage'

export interface DurableMutationDraft<T> {
  version: 2
  draft: T
  idempotencyKey: string
  keysByPayload: Record<string, string>
  blockedPayloads: Record<string, true>
  runtimeId: string
  keyRevision: number
  sessionRevision: number
}

export const SAFE_RETRY_STORAGE_ERROR =
  'Safe retry storage is unavailable; enable session storage and try again.'
export const SAFE_RETRY_RECOVERY_ERROR =
  'A prior request from another page lifecycle has an unresolved result; clear this tab’s site data and reopen the form before retrying.'

export class SafeRetryStorageError extends Error {
  constructor(message = SAFE_RETRY_STORAGE_ERROR) {
    super(message)
    this.name = 'SafeRetryStorageError'
  }
}

const retiredDraftKeys = new Map<string, Set<string>>()
const ALL_PAYLOADS_BLOCKED = '__all__'
const SAFE_RETRY_RUNTIME_ID =
  typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `runtime-${Date.now()}-${Math.random().toString(36).slice(2)}`

function clearRetiredDraftKeys(): void {
  retiredDraftKeys.clear()
}

subscribeApiKey(clearRetiredDraftKeys)

export function scopedMutationStorageKey(
  prefix: string,
  project: string | undefined,
  ...resources: Array<string | null>
): string {
  const scope = project === undefined ? ['global-scope'] : ['project', project]
  return `${prefix}:${JSON.stringify([scope, ...(resources.length > 0 ? resources : [null])])}`
}

function storage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.sessionStorage
  } catch {
    return null
  }
}

function persist<T>(storageKey: string, state: DurableMutationDraft<T>): boolean {
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

export function initializeDurableMutationDraft<T>(
  storageKey: string,
  fallback: T,
  validate: (value: unknown) => value is T,
  fingerprint: (draft: T) => string,
  createKey: () => string,
): DurableMutationDraft<T> {
  const keyRevision = getApiKeyRevision()
  const sessionRevision = getSessionRevision()
  let draft = fallback
  let keysByPayload: Record<string, string> = {}
  let blockedPayloads: Record<string, true> = {}
  let storedKey: string | null = null
  const retiredKeys = retiredDraftKeys.get(storageKey)
  try {
    const serialized = storage()?.getItem(storageKey)
    if (serialized) {
      const parsed = JSON.parse(serialized) as {
        version?: unknown
        draft?: unknown
        idempotencyKey?: unknown
        keysByPayload?: unknown
        blockedPayloads?: unknown
        runtimeId?: unknown
        keyRevision?: unknown
        sessionRevision?: unknown
      }
      const parsedKeys =
        parsed.keysByPayload &&
        typeof parsed.keysByPayload === 'object' &&
        !Array.isArray(parsed.keysByPayload)
          ? Object.fromEntries(
              Object.entries(parsed.keysByPayload).filter(
                (entry): entry is [string, string] =>
                  typeof entry[1] === 'string' && !retiredKeys?.has(entry[1]),
              ),
            )
          : {}
      const parsedBlocked =
        parsed.blockedPayloads &&
        typeof parsed.blockedPayloads === 'object' &&
        !Array.isArray(parsed.blockedPayloads)
          ? Object.fromEntries(
              Object.entries(parsed.blockedPayloads).filter(
                (entry): entry is [string, true] => entry[1] === true,
              ),
            )
          : {}
      const belongsToCurrentPrincipal = parsed.keyRevision === keyRevision
      const belongsToCurrentRuntime =
        parsed.version === 2 &&
        belongsToCurrentPrincipal &&
        parsed.runtimeId === SAFE_RETRY_RUNTIME_ID
      if (belongsToCurrentRuntime) {
        if (validate(parsed.draft)) draft = parsed.draft
        if (typeof parsed.idempotencyKey === 'string') storedKey = parsed.idempotencyKey
        keysByPayload = parsedKeys
        blockedPayloads = parsedBlocked
      } else if (
        parsed.version !== 2 ||
        parsed.runtimeId !== SAFE_RETRY_RUNTIME_ID
      ) {
        // A prior page may have sent any bound payload and then lost its
        // response. Block the whole form lifecycle rather than persisting its
        // reversible JSON fingerprints into the replacement page.
        if (
          Object.keys(parsedKeys).length > 0 ||
          Object.keys(parsedBlocked).length > 0
        ) {
          blockedPayloads = { [ALL_PAYLOADS_BLOCKED]: true }
        }
      }
    }
  } catch {
    // Ignore corrupt or unavailable session state and start a clean attempt.
  }
  if (storedKey && retiredKeys?.has(storedKey)) {
    // A successful completed form must not reappear as a draft merely because
    // its post-success storage cleanup failed.
    draft = fallback
    keysByPayload = {}
    storedKey = null
  }
  const signature = fingerprint(draft)
  const idempotencyKey = keysByPayload[signature] ?? storedKey ?? createKey()
  const state = {
    version: 2 as const,
    draft,
    idempotencyKey,
    keysByPayload,
    blockedPayloads,
    runtimeId: SAFE_RETRY_RUNTIME_ID,
    keyRevision,
    sessionRevision,
  }
  if (persist(storageKey, state)) retiredDraftKeys.delete(storageKey)
  return state
}

export function updateDurableMutationDraft<T>(
  storageKey: string,
  previous: DurableMutationDraft<T>,
  draft: T,
  fingerprint: (value: T) => string,
  createKey: () => string,
): DurableMutationDraft<T> {
  const previousSignature = fingerprint(previous.draft)
  const previousKeyWasSubmitted =
    previous.keysByPayload[previousSignature] === previous.idempotencyKey
  const signature = fingerprint(draft)
  const idempotencyKey =
    previous.keysByPayload[signature] ??
    (previousKeyWasSubmitted ? createKey() : previous.idempotencyKey)
  const state = {
    ...previous,
    draft,
    idempotencyKey,
  }
  persist(storageKey, state)
  return state
}

export function bindDurableMutationDraft<T>(
  storageKey: string,
  previous: DurableMutationDraft<T>,
  fingerprint: (value: T) => string,
): DurableMutationDraft<T> {
  const signature = fingerprint(previous.draft)
  if (
    previous.runtimeId !== SAFE_RETRY_RUNTIME_ID ||
    previous.keyRevision !== getApiKeyRevision() ||
    previous.sessionRevision !== getSessionRevision()
  ) {
    throw new SafeRetryStorageError(
      'Authentication changed while preparing the request; please retry.',
    )
  }
  if (
    previous.blockedPayloads[ALL_PAYLOADS_BLOCKED] ||
    previous.blockedPayloads[signature]
  ) {
    throw new SafeRetryStorageError(SAFE_RETRY_RECOVERY_ERROR)
  }
  const state = {
    ...previous,
    keysByPayload: {
      ...previous.keysByPayload,
      [signature]: previous.idempotencyKey,
    },
  }
  if (!persist(storageKey, state)) throw new SafeRetryStorageError()
  return state
}

export function clearDurableMutationDraft(storageKey: string, confirmedKey: string): void {
  const retired = retiredDraftKeys.get(storageKey) ?? new Set<string>()
  retired.add(confirmedKey)
  retiredDraftKeys.set(storageKey, retired)
  try {
    const target = storage()
    if (!target) return
    const serialized = target.getItem(storageKey)
    if (serialized !== null) {
      try {
        const parsed = JSON.parse(serialized) as { idempotencyKey?: unknown }
        if (parsed.idempotencyKey !== confirmedKey) return
      } catch {
        // A corrupt record cannot be safely reused and may be removed.
      }
    }
    target.removeItem(storageKey)
    if (target.getItem(storageKey) === null) retiredDraftKeys.delete(storageKey)
  } catch {
    // Keep the in-memory tombstone until a fresh key is durably persisted.
  }
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize)
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, canonicalize(item)]),
    )
  }
  return value
}

export function stableMutationFingerprint(value: unknown): string {
  return JSON.stringify(canonicalize(value))
}
