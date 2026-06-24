import { createContext, useContext } from 'react'

export interface DevDataModeContextValue {
  available: boolean
  enabled: boolean
  setEnabled: (enabled: boolean) => void
  reset: () => void
}

export const DevDataContext = createContext<DevDataModeContextValue>({
  available: false,
  enabled: false,
  setEnabled: () => undefined,
  reset: () => undefined,
})

export function useDevDataMode(): DevDataModeContextValue {
  return useContext(DevDataContext)
}

