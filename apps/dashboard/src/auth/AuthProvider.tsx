import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'

import {
  fetchSystemInfo,
  onUnauthorized,
  type ConsumerInfo,
  type SystemInfo,
} from '@/api/apexClient'
import { isApiError } from '@/api/errors'

import { clearApiKey, getApiKey, setApiKey, subscribeApiKey } from './keyStorage'

export type AuthState =
  | { status: 'no-key' }
  | { status: 'validating' }
  | { status: 'authenticated'; consumer: ConsumerInfo; systemInfo: SystemInfo }
  | { status: 'error'; message: string }

export interface AuthContextValue {
  state: AuthState
  submitKey: (key: string) => void
  signOut: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

/**
 * Tracks the stored API key and validates every key change against
 * GET /v1/system/info; the returned consumer identity feeds the sidebar
 * footer and role gating. A 401 anywhere clears the stored key.
 *
 * `staticState` bypasses validation entirely — test seam only.
 */
export function AuthProvider({
  children,
  staticState,
}: {
  children: ReactNode
  staticState?: AuthState
}) {
  const [state, setState] = useState<AuthState>(() =>
    staticState ?? (getApiKey() ? { status: 'validating' } : { status: 'no-key' }),
  )
  const attemptRef = useRef(0)

  const validate = useCallback(async () => {
    const attempt = ++attemptRef.current
    setState({ status: 'validating' })
    try {
      const info = await fetchSystemInfo()
      if (attempt !== attemptRef.current) return
      setState({ status: 'authenticated', consumer: info.consumer, systemInfo: info })
    } catch (error) {
      if (attempt !== attemptRef.current) return
      if (isApiError(error) && error.status === 401) {
        clearApiKey()
        setState({ status: 'error', message: 'API key was rejected. Check the key and try again.' })
      } else if (isApiError(error)) {
        setState({ status: 'error', message: error.message })
      } else {
        setState({ status: 'error', message: 'Unable to reach the APEX API. Check connectivity and try again.' })
      }
    }
  }, [])

  useEffect(() => {
    if (staticState) return

    const unsubscribeKey = subscribeApiKey((key) => {
      if (key) {
        void validate()
      } else {
        // Keep an error visible (e.g. rejected key) instead of flashing back to the bare gate.
        setState((prev) => (prev.status === 'error' ? prev : { status: 'no-key' }))
      }
    })
    const unsubscribeUnauthorized = onUnauthorized(() => {
      clearApiKey()
    })

    if (getApiKey()) void validate()

    return () => {
      unsubscribeKey()
      unsubscribeUnauthorized()
    }
  }, [staticState, validate])

  const submitKey = useCallback((key: string) => {
    setApiKey(key)
  }, [])

  const signOut = useCallback(() => {
    attemptRef.current += 1
    clearApiKey()
    setState({ status: 'no-key' })
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({ state: staticState ?? state, submitKey, signOut }),
    [staticState, state, submitKey, signOut],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider')
  return ctx
}

export function useConsumer(): ConsumerInfo | null {
  const { state } = useAuth()
  return state.status === 'authenticated' ? state.consumer : null
}
