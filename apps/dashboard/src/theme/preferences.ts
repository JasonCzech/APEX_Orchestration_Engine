import { useSyncExternalStore } from 'react'

export const PREFERENCE_KEY = 'apex.preferences.v1'
export interface Preferences {
  reduceMotion: boolean
  rows: 10 | 25 | 50 | 100
  wordWrap: boolean
  timeZone: 'local' | 'UTC'
}
const defaults: Preferences = { reduceMotion: false, rows: 10, wordWrap: true, timeZone: 'local' }
const listeners = new Set<() => void>()
let cachedRaw: string | null | undefined
let cached = defaults
let sessionOnly = false
function read(): Preferences {
  if (sessionOnly) return cached
  let raw: string | null = null
  try {
    raw = localStorage.getItem(PREFERENCE_KEY)
  } catch {
    return cached
  }
  if (raw === cachedRaw) return cached
  cachedRaw = raw
  try {
    const value = JSON.parse(raw ?? '{}') as Partial<Preferences>
    cached = {
      reduceMotion: value?.reduceMotion === true,
      rows: value?.rows === 25 || value?.rows === 50 || value?.rows === 100 ? value.rows : 10,
      wordWrap: value?.wordWrap !== false,
      timeZone: value?.timeZone === 'UTC' ? 'UTC' : 'local',
    }
  } catch {
    cached = defaults
  }
  return cached
}
function subscribe(listener: () => void) {
  listeners.add(listener)
  const onStorage = (event: StorageEvent) => {
    if (!event.key || event.key === PREFERENCE_KEY) listener()
  }
  window.addEventListener('storage', onStorage)
  return () => {
    listeners.delete(listener)
    window.removeEventListener('storage', onStorage)
  }
}
export function usePreferences() {
  const preferences = useSyncExternalStore(subscribe, read, () => defaults)
  return {
    preferences,
    setPreferences(patch: Partial<Preferences>) {
      cached = { ...read(), ...patch }
      try {
        cachedRaw = JSON.stringify(cached)
        localStorage.setItem(PREFERENCE_KEY, cachedRaw)
      } catch {
        sessionOnly = true
      }
      document.documentElement.dataset.reduceMotion = String(cached.reduceMotion)
      listeners.forEach((listener) => listener())
    },
  }
}
export const readPreferences = read

/** Refresh timestamp consumers without remounting their forms or route. */
export function useTimestampPreference() {
  return useSyncExternalStore(subscribe, () => read().timeZone, () => defaults.timeZone)
}
