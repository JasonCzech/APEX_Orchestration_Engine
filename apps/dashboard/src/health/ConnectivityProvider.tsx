import { createContext, useContext, useEffect, useMemo, type ReactNode } from 'react'

import { useAuth } from '@/auth/AuthProvider'
import { isDevAuthEnabled } from '@/auth/devAuth'

import { useSystemHealth, type ConnectivityStatus } from './useSystemHealth'

interface ConnectivityContextValue {
  status: ConnectivityStatus
}

const ConnectivityContext = createContext<ConnectivityContextValue>({ status: 'unknown' })

/** Health polling runs only with a validated session; the sidebar status dot consumes it. */
export function ConnectivityProvider({ children }: { children: ReactNode }) {
  const { state, reconcileSystemInfo } = useAuth()
  const { status, systemInfo } = useSystemHealth(state.status === 'authenticated')
  useEffect(() => {
    if (systemInfo && !isDevAuthEnabled()) reconcileSystemInfo(systemInfo)
  }, [systemInfo, reconcileSystemInfo])
  const value = useMemo(() => ({ status }), [status])
  return <ConnectivityContext.Provider value={value}>{children}</ConnectivityContext.Provider>
}

export function useConnectivity(): ConnectivityContextValue {
  return useContext(ConnectivityContext)
}
