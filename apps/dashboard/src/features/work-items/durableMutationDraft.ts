export interface DurableMutationDraft<T> {
  draft: T
  idempotencyKey: string
  keysByPayload: Record<string, string>
}

function storage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.sessionStorage
  } catch {
    return null
  }
}

function persist<T>(storageKey: string, state: DurableMutationDraft<T>): void {
  try {
    storage()?.setItem(storageKey, JSON.stringify(state))
  } catch {
    // Mutations still work when storage is unavailable or full; only durable
    // browser retry recovery is degraded for that session.
  }
}

export function initializeDurableMutationDraft<T>(
  storageKey: string,
  fallback: T,
  validate: (value: unknown) => value is T,
  fingerprint: (draft: T) => string,
  createKey: () => string,
): DurableMutationDraft<T> {
  let draft = fallback
  let keysByPayload: Record<string, string> = {}
  let storedKey: string | null = null
  try {
    const serialized = storage()?.getItem(storageKey)
    if (serialized) {
      const parsed = JSON.parse(serialized) as {
        draft?: unknown
        idempotencyKey?: unknown
        keysByPayload?: unknown
      }
      if (validate(parsed.draft)) draft = parsed.draft
      if (typeof parsed.idempotencyKey === 'string') storedKey = parsed.idempotencyKey
      if (
        parsed.keysByPayload &&
        typeof parsed.keysByPayload === 'object' &&
        !Array.isArray(parsed.keysByPayload)
      ) {
        keysByPayload = Object.fromEntries(
          Object.entries(parsed.keysByPayload).filter(
            (entry): entry is [string, string] => typeof entry[1] === 'string',
          ),
        )
      }
    }
  } catch {
    // Ignore corrupt or unavailable session state and start a clean attempt.
  }
  const signature = fingerprint(draft)
  const idempotencyKey = keysByPayload[signature] ?? storedKey ?? createKey()
  const state = {
    draft,
    idempotencyKey,
    keysByPayload,
  }
  persist(storageKey, state)
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
    draft,
    idempotencyKey,
    keysByPayload: previous.keysByPayload,
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
  const state = {
    ...previous,
    keysByPayload: {
      ...previous.keysByPayload,
      [signature]: previous.idempotencyKey,
    },
  }
  persist(storageKey, state)
  return state
}

export function clearDurableMutationDraft(storageKey: string): void {
  try {
    storage()?.removeItem(storageKey)
  } catch {
    // Successful server completion is authoritative even if cleanup fails.
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
