export const API_KEY_STORAGE_KEY = 'apex.apiKey'

type KeyListener = (key: string | null) => void

const listeners = new Set<KeyListener>()

function safeStorage(): Storage | null {
  try {
    return window.localStorage
  } catch {
    return null
  }
}

export function getApiKey(): string | null {
  const stored = safeStorage()?.getItem(API_KEY_STORAGE_KEY) ?? null
  return stored && stored.length > 0 ? stored : null
}

export function setApiKey(key: string): void {
  safeStorage()?.setItem(API_KEY_STORAGE_KEY, key)
  notify(key)
}

export function clearApiKey(): void {
  safeStorage()?.removeItem(API_KEY_STORAGE_KEY)
  notify(null)
}

export function subscribeApiKey(listener: KeyListener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

function notify(key: string | null): void {
  for (const listener of listeners) listener(key)
}
