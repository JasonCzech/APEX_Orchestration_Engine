import {
  createContext,
  Fragment,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'

import { useQueryClient } from '@tanstack/react-query'

import {
  fetchSystemInfo,
  onUnauthorized,
  type ConsumerInfo,
  type SystemInfo,
} from '@/api/apexClient'
import { isApiError } from '@/api/errors'

import { getDevSystemInfo, isDevAuthEnabled } from './devAuth'
import {
  bumpSessionRevision,
  clearApiKey,
  getApiKey,
  setApiKey,
  subscribeApiKey,
} from './keyStorage'

export type AuthState =
  | { status: 'no-key' }
  | { status: 'validating' }
  | { status: 'authenticated'; consumer: ConsumerInfo; systemInfo: SystemInfo }
  | { status: 'error'; message: string }

export interface AuthContextValue {
  state: AuthState
  submitKey: (key: string) => void
  signOut: () => void
  reconcileSystemInfo: (info: SystemInfo) => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

function createDevAuthState(): AuthState {
  const systemInfo = getDevSystemInfo()
  return { status: 'authenticated', consumer: systemInfo.consumer, systemInfo }
}

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
    staticState ??
    (isDevAuthEnabled()
      ? createDevAuthState()
      : getApiKey()
        ? { status: 'validating' }
        : { status: 'no-key' }),
  )
  const stateRef = useRef(state)
  stateRef.current = state
  const attemptRef = useRef(0)
  const suppressNextKeyEventRef = useRef(false)
  const [authEpoch, setAuthEpoch] = useState(0)
  const queryClient = useQueryClient()

  const clearSessionCache = useCallback(() => {
    // Cancellation prevents old-session responses from repopulating query
    // observers; clear() removes both query data and mutation state, including
    // artifact blobs with an infinite stale time.
    void queryClient.cancelQueries()
    queryClient.clear()
  }, [queryClient])

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
        // Set the visible failure before clearing storage; the storage listener
        // intentionally preserves an existing error state.
        suppressNextKeyEventRef.current = true
        setState({ status: 'error', message: 'API key was rejected. Check the key and try again.' })
        clearApiKey()
      } else if (isApiError(error)) {
        setState({ status: 'error', message: error.message })
      } else {
        setState({ status: 'error', message: 'Unable to reach the APEX API. Check connectivity and try again.' })
      }
    }
  }, [])

  useEffect(() => {
    if (staticState) return
    if (isDevAuthEnabled()) {
      setState(createDevAuthState())
      return
    }

    const unsubscribeKey = subscribeApiKey((key) => {
      if (suppressNextKeyEventRef.current) {
        suppressNextKeyEventRef.current = false
        return
      }
      // Invalidate every in-flight validation before reacting to a storage
      // event.  Storage events are asynchronous, so a request started under
      // the previous key may otherwise authenticate after sign-out.
      attemptRef.current += 1
      setAuthEpoch((epoch) => epoch + 1)
      clearSessionCache()
      if (key) {
        void validate()
      } else {
        // Keep an error visible (e.g. rejected key) instead of flashing back to the bare gate.
        setState((prev) => (prev.status === 'error' ? prev : { status: 'no-key' }))
      }
    })
    const unsubscribeUnauthorized = onUnauthorized(() => {
      if (stateRef.current.status === 'validating') {
        suppressNextKeyEventRef.current = true
        setState({ status: 'error', message: 'API key was rejected. Check the key and try again.' })
      }
      clearApiKey()
    })

    if (getApiKey()) {
      clearSessionCache()
      void validate()
    }

    return () => {
      unsubscribeKey()
      unsubscribeUnauthorized()
    }
  }, [clearSessionCache, staticState, validate])

  const submitKey = useCallback((key: string) => {
    setApiKey(key)
  }, [])

  const signOut = useCallback(() => {
    attemptRef.current += 1
    // Dev auth deliberately has no key subscription, so clear explicitly in
    // that mode. Production sessions clear synchronously via subscribeApiKey.
    if (isDevAuthEnabled()) clearSessionCache()
    clearApiKey()
    setState(isDevAuthEnabled() ? createDevAuthState() : { status: 'no-key' })
  }, [clearSessionCache])

  const reconcileSystemInfo = useCallback(
    (info: SystemInfo) => {
      const previous = stateRef.current
      if (previous.status !== 'authenticated') return
      const beforeScopes = JSON.stringify(
        [...previous.consumer.scopes].sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b))),
      )
      const afterScopes = JSON.stringify(
        [...info.consumer.scopes].sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b))),
      )
      const identityChanged =
        previous.consumer.name !== info.consumer.name ||
        previous.consumer.role !== info.consumer.role ||
        beforeScopes !== afterScopes
      if (identityChanged) {
        bumpSessionRevision()
        clearSessionCache()
        setAuthEpoch((epoch) => epoch + 1)
      }
      setState({ status: 'authenticated', consumer: info.consumer, systemInfo: info })
    },
    [clearSessionCache],
  )

  const value = useMemo<AuthContextValue>(
    () => ({ state: staticState ?? state, submitKey, signOut, reconcileSystemInfo }),
    [staticState, state, submitKey, signOut, reconcileSystemInfo],
  )

  const consumer = value.state.status === 'authenticated' ? value.state.consumer : null
  const sessionRenderKey = staticState
    ? 'static-auth'
    : `${authEpoch}:${value.state.status}:${consumer?.name ?? 'anonymous'}`
  return (
    <AuthContext.Provider value={value}>
      <Fragment key={sessionRenderKey}>{children}</Fragment>
    </AuthContext.Provider>
  )
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

/** Optional variant for reusable controls that are also rendered in isolation by tests. */
export function useOptionalConsumer(): ConsumerInfo | null | undefined {
  const ctx = useContext(AuthContext)
  if (!ctx) return undefined
  return ctx.state.status === 'authenticated' ? ctx.state.consumer : null
}
