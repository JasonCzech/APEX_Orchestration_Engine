import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App'
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
      <App />
    </StrictMode>,
  )
}

void bootstrap()
