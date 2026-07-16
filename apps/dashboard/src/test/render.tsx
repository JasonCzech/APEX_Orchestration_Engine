import { render } from '@testing-library/react'
import { flushSync } from 'react-dom'
import { createMemoryRouter, RouterProvider } from 'react-router'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { Role } from '@/api/apexClient'
import { queryKeys } from '@/api/queryKeys'
import { ApiKeyGate } from '@/auth/ApiKeyGate'
import { AuthProvider, type AuthState } from '@/auth/AuthProvider'
import { TopbarContributionProvider } from '@/components/layout/TopbarContributionProvider'
import { DevDataProvider } from '@/dev-data'
import { ConnectivityProvider } from '@/health/ConnectivityProvider'
import { appRoutes } from '@/routes/router'
import { ThemeProvider } from '@/theme/useTheme'

import { SYSTEM_INFO } from './server'

/** No-retry client so error states surface immediately in tests. */
export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  })
}

/** Pre-validated session for tests that start past the ApiKeyGate. */
export function authenticatedState(role: Role = 'admin', name = 'Dash Ops', scopes = SYSTEM_INFO.consumer.scopes): AuthState {
  const consumer = { name, role, scopes }
  return {
    status: 'authenticated',
    consumer,
    systemInfo: { ...SYSTEM_INFO, consumer },
  }
}

/**
 * Mounts the full provider stack (App.tsx shape) on a memory router over the
 * real appRoutes. `authState` short-circuits key validation when a test wants
 * to start inside the shell.
 */
export function renderApp({
  initialEntries = ['/'],
  authState,
  queryClient = createTestQueryClient(),
  seedSystemInfo = true,
}: {
  initialEntries?: string[]
  authState?: AuthState
  queryClient?: QueryClient
  /** Set false only when the test is exercising the connectivity request itself. */
  seedSystemInfo?: boolean
} = {}) {
  // A static authenticated session is already pre-validated. Seed the matching
  // query so unrelated full-shell tests do not race an extra MSW response body
  // against the route request they actually assert. Connectivity tests opt out.
  if (seedSystemInfo && authState?.status === 'authenticated') {
    queryClient.setQueryData(queryKeys.system.info(), authState.systemInfo)
  }
  const router = createMemoryRouter(appRoutes, { initialEntries })
  const result = render(
    <QueryClientProvider client={queryClient}>
      <DevDataProvider>
        <AuthProvider staticState={authState}>
          <ConnectivityProvider>
            <ThemeProvider>
              <TopbarContributionProvider>
                <ApiKeyGate>
                  <RouterProvider
                    router={router}
                    flushSync={(callback) => {
                      flushSync(callback)
                    }}
                  />
                </ApiKeyGate>
              </TopbarContributionProvider>
            </ThemeProvider>
          </ConnectivityProvider>
        </AuthProvider>
      </DevDataProvider>
    </QueryClientProvider>,
  )
  return { ...result, router, queryClient }
}
