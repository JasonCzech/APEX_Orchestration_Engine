import { isDevAuthEnabled } from '@/auth/devAuth'
import { isDemoDataMode } from '@/config/runtimeConfig'

import { createDevDataStore, type DevDataStore } from './store'

export const DEV_DATA_STORAGE_KEY = 'apex.devData.enabled'

type Listener = () => void

const listeners = new Set<Listener>()
let store: DevDataStore | null = null

function safeStorage(): Storage | null {
  try {
    return window.localStorage
  } catch {
    return null
  }
}

function notify(): void {
  for (const listener of listeners) listener()
}

export function isDevDataAvailable(): boolean {
  return isDemoDataMode() || (import.meta.env.DEV && isDevAuthEnabled())
}

export function isDevDataEnabled(): boolean {
  if (!isDevDataAvailable()) return false
  if (isDemoDataMode()) return true
  return safeStorage()?.getItem(DEV_DATA_STORAGE_KEY) === 'true'
}

export function getDevDataStore(): DevDataStore | null {
  if (!isDevDataEnabled()) return null
  store ??= createDevDataStore()
  return store
}

export function setDevDataEnabled(enabled: boolean): void {
  if (isDemoDataMode()) {
    if (enabled) store ??= createDevDataStore()
    notify()
    return
  }
  const storage = safeStorage()
  if (enabled && isDevDataAvailable()) {
    storage?.setItem(DEV_DATA_STORAGE_KEY, 'true')
    store = createDevDataStore()
  } else {
    storage?.removeItem(DEV_DATA_STORAGE_KEY)
    store = null
  }
  notify()
}

export function resetDevDataStore(): void {
  const storage = safeStorage()
  if (storage) {
    for (let index = storage.length - 1; index >= 0; index -= 1) {
      const key = storage.key(index)
      if (key === 'apex.demo.state.v1' || key?.startsWith('apex.idempotency.')) storage.removeItem(key)
    }
  }
  try {
    const session = window.sessionStorage
    for (let index = session.length - 1; index >= 0; index -= 1) {
      const key = session.key(index)
      if (key?.startsWith('apex:stream:last-event-id') || key?.startsWith('apex.idempotency.')) session.removeItem(key)
    }
  } catch {
    // Reset remains useful when session storage is unavailable.
  }
  if (isDevDataEnabled()) store = createDevDataStore()
  notify()
}

export function subscribeDevDataMode(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}
