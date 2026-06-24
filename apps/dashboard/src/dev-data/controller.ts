import { isDevAuthEnabled } from '@/auth/devAuth'

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
  return import.meta.env.DEV && isDevAuthEnabled()
}

export function isDevDataEnabled(): boolean {
  if (!isDevDataAvailable()) return false
  return safeStorage()?.getItem(DEV_DATA_STORAGE_KEY) === 'true'
}

export function getDevDataStore(): DevDataStore | null {
  if (!isDevDataEnabled()) return null
  store ??= createDevDataStore()
  return store
}

export function setDevDataEnabled(enabled: boolean): void {
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
  if (isDevDataEnabled()) store = createDevDataStore()
  notify()
}

export function subscribeDevDataMode(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

