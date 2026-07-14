import { getDevApiKey, isDevAuthEnabled } from './devAuth'

export const API_KEY_STORAGE_KEY = 'apex.apiKey'

type KeyListener = (key: string | null) => void

const listeners = new Set<KeyListener>()
let keyRevision = 0
let storageListenerAttached = false

function safeStorage(): Storage | null {
  try {
    return window.localStorage
  } catch {
    return null
  }
}

export function getApiKey(): string | null {
  if (isDevAuthEnabled()) return getDevApiKey()
  try {
    const stored = safeStorage()?.getItem(API_KEY_STORAGE_KEY) ?? null
    return stored && stored.length > 0 ? stored : null
  } catch {
    return null
  }
}

export function setApiKey(key: string): void {
  try {
    safeStorage()?.setItem(API_KEY_STORAGE_KEY, key)
  } catch {
    // Keep the in-memory revision/listener transition usable when persistence is blocked.
  }
  keyRevision += 1
  notify(key)
}

export function clearApiKey(): void {
  try {
    safeStorage()?.removeItem(API_KEY_STORAGE_KEY)
  } catch {
    // Sign-out must still invalidate the in-memory session if storage is unavailable.
  }
  keyRevision += 1
  notify(null)
}

/**
 * Monotonic credential generation for rejecting responses that were sent with
 * an older session. Comparing only the key value is insufficient when a user
 * rotates away from a key and later rotates back to it.
 */
export function getApiKeyRevision(): number {
  return keyRevision
}

export function subscribeApiKey(listener: KeyListener): () => void {
  listeners.add(listener)
  if (!storageListenerAttached && typeof window !== 'undefined') {
    window.addEventListener('storage', handleStorageChange)
    storageListenerAttached = true
  }
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0 && storageListenerAttached && typeof window !== 'undefined') {
      window.removeEventListener('storage', handleStorageChange)
      storageListenerAttached = false
    }
  }
}

/** Browser storage events fire only in the other tabs sharing this origin. */
function handleStorageChange(event: StorageEvent): void {
  if (isDevAuthEnabled()) return
  if (event.key !== null && event.key !== API_KEY_STORAGE_KEY) return
  const storage = safeStorage()
  if (event.storageArea !== null && storage !== null && event.storageArea !== storage) return
  const key =
    event.key === API_KEY_STORAGE_KEY
      ? event.newValue && event.newValue.length > 0
        ? event.newValue
        : null
      : getApiKey()
  keyRevision += 1
  notify(key)
}

function notify(key: string | null): void {
  for (const listener of listeners) listener(key)
}
