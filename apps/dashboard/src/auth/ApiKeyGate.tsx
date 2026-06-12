import { useState, type FormEvent, type ReactNode } from 'react'

import { useAuth } from './AuthProvider'
import './ApiKeyGate.css'

/**
 * Full-screen key-entry card shown whenever there is no validated session
 * (no stored key, validation in flight after submit, or a rejected key).
 * Keys are validated against GET /v1/system/info before the shell renders.
 */
export function ApiKeyGate({ children }: { children: ReactNode }) {
  const { state, submitKey } = useAuth()
  const [draft, setDraft] = useState('')

  if (state.status === 'authenticated') return <>{children}</>

  const validating = state.status === 'validating'

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const candidate = draft.trim()
    if (candidate) submitKey(candidate)
  }

  return (
    <div className="api-key-gate">
      <section className="api-key-gate-card glass-panel animate-enter">
        <p className="api-key-gate-kicker">APEX Orchestration</p>
        <h1>Connect to the control plane</h1>
        <p className="api-key-gate-copy">
          Enter your consumer API key. It is validated against the APEX system endpoint and stored
          only in this browser.
        </p>
        <form className="api-key-gate-form" onSubmit={handleSubmit}>
          <label className="api-key-gate-label" htmlFor="api-key-input">
            API key
          </label>
          <input
            id="api-key-input"
            className="api-key-gate-input"
            type="password"
            autoComplete="off"
            placeholder="apex_…"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            disabled={validating}
          />
          <button
            type="submit"
            className="btn btn-primary"
            disabled={validating || draft.trim() === ''}
          >
            {validating ? 'Validating…' : 'Connect'}
          </button>
        </form>
        {state.status === 'error' && (
          <p className="api-key-gate-error" role="alert">
            {state.message}
          </p>
        )}
      </section>
    </div>
  )
}
