import { createContext, useContext, useMemo, type ReactNode } from 'react'

import { useAuth } from '@/auth/AuthProvider'

import { useSystemHealth, type ConnectivityStatus } from './useSystemHealth'

interface ConnectivityContextValue {
  status: ConnectivityStatus
}

const ConnectivityContext = createContext<ConnectivityContextValue>({ status: 'unknown' })

/** Health polling runs only with a validated session; the sidebar status dot consumes it. */
export function ConnectivityProvider({ children }: { children: ReactNode }) {
  const { state } = useAuth()
  const { status } = useSystemHealth(state.status === 'authenticated')
  const value = useMemo(() => ({ status }), [status])
  return <ConnectivityContext.Provider value={value}>{children}</ConnectivityContext.Provider>
}

export function useConnectivity(): ConnectivityContextValue {
  return useContext(ConnectivityContext)
}
