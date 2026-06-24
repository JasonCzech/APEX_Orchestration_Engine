import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App'
import { AppErrorBoundary } from './AppErrorBoundary'
import { loadRuntimeConfig } from './config/runtimeConfig'
import './theme/index.css'

/**
 * Runtime config (/config.json) is fetched before mount so API clients are
 * born with the right origins — no rebuild per environment. Missing config
 * falls back to same-origin defaults (vite proxy in dev, reverse proxy in
 * production).
 */
async function bootstrap() {
  await loadRuntimeConfig()

  const container = document.getElementById('root')
  if (!container) throw new Error('Root container #root is missing from index.html')

  createRoot(container).render(
    <StrictMode>
      <AppErrorBoundary>
        <App />
      </AppErrorBoundary>
    </StrictMode>,
  )
}

void bootstrap().catch((error: unknown) => {
  const container = document.getElementById('root')
  if (!container) throw error
  const message = error instanceof Error ? error.message : 'The dashboard could not start.'
  createRoot(container).render(
    <StrictMode>
      <AppErrorBoundary>
        <div className="app-shell">
          <main className="app-main">
            <section className="problem-card glass-panel" role="alert">
              <h2>Something went wrong</h2>
              <p>{message}</p>
            </section>
          </main>
        </div>
      </AppErrorBoundary>
    </StrictMode>,
  )
})
