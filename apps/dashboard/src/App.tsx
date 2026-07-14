import { useMemo, useSyncExternalStore } from 'react'
import { RouterProvider } from 'react-router'

import { QueryClientProvider } from '@tanstack/react-query'

import { createQueryClient } from '@/api/queryClient'
import { getApiKeyRevision, subscribeApiKey } from '@/auth/keyStorage'
import { ApiKeyGate } from '@/auth/ApiKeyGate'
import { AuthProvider } from '@/auth/AuthProvider'
import { TopbarContributionProvider } from '@/components/layout/TopbarContributionProvider'
import { DevDataProvider } from '@/dev-data'
import { ConnectivityProvider } from '@/health/ConnectivityProvider'
import { createAppRouter } from '@/routes/router'
import { ThemeProvider } from '@/theme/useTheme'

/**
 * Thin composition root: providers + router. The ApiKeyGate holds the shell
 * back until GET /v1/system/info validates the stored key.
 */
export default function App() {
  const authRevision = useSyncExternalStore(
    (onStoreChange) => subscribeApiKey(() => onStoreChange()),
    getApiKeyRevision,
    getApiKeyRevision,
  )
  const queryClient = useMemo(createQueryClient, [authRevision])
  const router = useMemo(createAppRouter, [])

  return (
    <QueryClientProvider client={queryClient}>
      <DevDataProvider>
        <AuthProvider>
          <ConnectivityProvider>
            <ThemeProvider>
              <TopbarContributionProvider>
                <ApiKeyGate>
                  <RouterProvider router={router} />
                </ApiKeyGate>
              </TopbarContributionProvider>
            </ThemeProvider>
          </ConnectivityProvider>
        </AuthProvider>
      </DevDataProvider>
    </QueryClientProvider>
  )
}
