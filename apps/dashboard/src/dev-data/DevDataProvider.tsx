import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'

import { useQueryClient } from '@tanstack/react-query'

import { DevDataContext, type DevDataModeContextValue } from './DevDataContext'
import {
  isDevDataAvailable,
  isDevDataEnabled,
  resetDevDataStore,
  setDevDataEnabled,
  subscribeDevDataMode,
} from './controller'

function readSnapshot() {
  return { available: isDevDataAvailable(), enabled: isDevDataEnabled() }
}

export function DevDataProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const [snapshot, setSnapshot] = useState(readSnapshot)

  useEffect(() => {
    return subscribeDevDataMode(() => {
      setSnapshot(readSnapshot())
      void queryClient.invalidateQueries()
      void queryClient.refetchQueries({ type: 'active' })
    })
  }, [queryClient])

  const setEnabled = useCallback((enabled: boolean) => {
    setDevDataEnabled(enabled)
  }, [])

  const reset = useCallback(() => {
    resetDevDataStore()
  }, [])

  const value = useMemo<DevDataModeContextValue>(
    () => ({ ...snapshot, setEnabled, reset }),
    [reset, setEnabled, snapshot],
  )

  return <DevDataContext.Provider value={value}>{children}</DevDataContext.Provider>
}
