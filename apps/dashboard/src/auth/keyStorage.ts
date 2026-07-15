import { getDevApiKey, isDevAuthEnabled } from './devAuth'

export const API_KEY_STORAGE_KEY = 'apex.apiKey'

type KeyListener = (key: string | null) => void

const listeners = new Set<KeyListener>()
let keyRevision = 0
let sessionRevision = 0
let memoryKey: string | null = null
let memoryOnly = false
let storageListenerAttached = false
const sessionListeners = new Set<() => void>()

function safeStorage(): Storage | null {
  try {
    return window.localStorage
  } catch {
    return null
  }
}

export function getApiKey(): string | null {
  if (isDevAuthEnabled()) return getDevApiKey()
  // Once a persistence write fails, the in-memory transition is authoritative.
  // Reading an older value successfully does not mean the failed write recovered.
  if (memoryOnly) return memoryKey
  try {
    const storage = safeStorage()
    if (!storage) {
      memoryOnly = true
      return memoryKey
    }
    const stored = storage.getItem(API_KEY_STORAGE_KEY)
    memoryKey = stored && stored.length > 0 ? stored : null
    return memoryKey
  } catch {
    memoryOnly = true
    return memoryKey
  }
}

export function setApiKey(key: string): void {
  memoryKey = key
  try {
    const storage = safeStorage()
    if (!storage) throw new Error('localStorage unavailable')
    storage.setItem(API_KEY_STORAGE_KEY, key)
    memoryOnly = false
  } catch {
    // Keep the in-memory revision/listener transition usable when persistence is blocked.
    memoryOnly = true
  }
  keyRevision += 1
  notify(key)
}

export function clearApiKey(): void {
  memoryKey = null
  try {
    const storage = safeStorage()
    if (!storage) throw new Error('localStorage unavailable')
    storage.removeItem(API_KEY_STORAGE_KEY)
    memoryOnly = false
  } catch {
    // Sign-out must still invalidate the in-memory session if storage is unavailable.
    memoryOnly = true
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

/** Revision for semantic identity changes that do not rotate the API key. */
export function getSessionRevision(): number {
  return sessionRevision
}

export function bumpSessionRevision(): void {
  sessionRevision += 1
  for (const listener of sessionListeners) listener()
}

export function subscribeSession(listener: () => void): () => void {
  sessionListeners.add(listener)
  return () => sessionListeners.delete(listener)
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
  memoryKey = key
  memoryOnly = false
  keyRevision += 1
  notify(key)
}

function notify(key: string | null): void {
  for (const listener of listeners) listener(key)
}
