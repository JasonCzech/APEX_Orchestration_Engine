/**
 * /settings — local workstation preferences + session controls (plan Part 2
 * route table). Four sections:
 *  - Theme: the 5 APEX Load themes as preview swatch cards (data-theme via the
 *    existing useTheme provider, persisted to localStorage).
 *  - API key: masked display of the stored browser key, replace-with-validate
 *    (GET /v1/system/info with the candidate key BEFORE saving), sign out.
 *  - Connection: runtime-config origins (read-only) + live connectivity dot.
 *  - About: system name/version/environment + the validated consumer identity.
 */
import { useEffect, useRef, useState, useSyncExternalStore, type FormEvent } from 'react'

import { useAuth } from '@/auth/AuthProvider'
import {
  getApiKey,
  getApiKeyRevision,
  setApiKey,
  subscribeApiKey,
} from '@/auth/keyStorage'
import { getRuntimeConfig, resolveApexBaseUrl } from '@/config/runtimeConfig'
import { useConnectivity } from '@/health/ConnectivityProvider'
import { THEME_LABELS, useTheme, type ThemeName } from '@/theme/useTheme'

import './settings.css'

/**
 * Per-theme preview palettes for the swatch cards. The theme blocks are
 * :root-scoped ([data-theme] on <html>), so a nested preview cannot inherit
 * another theme's CSS variables — these literals mirror theme/tokens.css +
 * theme/themes.css (bg-primary, bg-secondary, accent, text-primary).
 */
const THEME_PREVIEWS: Record<ThemeName, { bg: string; surface: string; accent: string; text: string }> = {
  'apex-light': { bg: '#f5f7fa', surface: '#ffffff', accent: '#00a8e0', text: '#1a1a2e' },
  dark: { bg: '#050508', surface: '#0b0c10', accent: '#8b5cf6', text: '#f8fafc' },
  light: { bg: '#f8fafc', surface: '#f1f5f9', accent: '#4f46e5', text: '#0f172a' },
  'solarized-dark': { bg: '#002b36', surface: '#073642', accent: '#268bd2', text: '#fdf6e3' },
  'solarized-light': { bg: '#fdf6e3', surface: '#eee8d5', accent: '#859900', text: '#002b36' },
  'monokai-dimmed': { bg: '#1e1f1c', surface: '#272822', accent: '#ff6188', text: '#f8f8f2' },
}

function ThemeSection() {
  const { theme, themes, setTheme } = useTheme()
  return (
    <section className="glass-panel settings-section" aria-label="Theme">
      <h3 className="settings-section-title">Theme</h3>
      <div className="settings-theme-grid" role="group" aria-label="Theme picker">
        {themes.map((name) => {
          const preview = THEME_PREVIEWS[name]
          const selected = theme === name
          return (
            <button
              key={name}
              type="button"
              className={`settings-theme-card${selected ? ' settings-theme-card--selected' : ''}`}
              aria-pressed={selected}
              onClick={() => setTheme(name)}
            >
              <span
                className="settings-theme-swatch"
                style={{ background: preview.bg }}
                aria-hidden="true"
              >
                <span className="settings-swatch-bar" style={{ background: preview.surface }} />
                <span className="settings-swatch-dot" style={{ background: preview.accent }} />
                <span className="settings-swatch-line" style={{ background: preview.text }} />
              </span>
              <span className="settings-theme-name">{THEME_LABELS[name]}</span>
              {selected && <span className="topbar-meta-chip accent">active</span>}
            </button>
          )
        })}
      </div>
    </section>
  )
}

function maskKey(key: string): string {
  return `••••••••${key.slice(-4)}`
}

type ReplaceState =
  | { kind: 'idle' }
  | { kind: 'validating' }
  | { kind: 'saved' }
  | { kind: 'error'; message: string }

function ApiKeySection() {
  const { signOut } = useAuth()
  const storedKey = useSyncExternalStore(subscribeApiKey, getApiKey)
  const [draft, setDraft] = useState('')
  const [state, setState] = useState<ReplaceState>({ kind: 'idle' })
  const validationRef = useRef<AbortController | null>(null)

  useEffect(
    () => () => {
      validationRef.current?.abort()
    },
    [],
  )

  const validating = state.kind === 'validating'
  const canSubmit = draft.trim() !== '' && !validating

  async function replaceKey(event: FormEvent) {
    event.preventDefault()
    const candidate = draft.trim()
    if (!candidate) return
    validationRef.current?.abort()
    const controller = new AbortController()
    const authRevision = getApiKeyRevision()
    validationRef.current = controller
    setState({ kind: 'validating' })
    try {
      // Validate BEFORE persisting: the active session must survive a typo.
      const response = await fetch(`${resolveApexBaseUrl()}/v1/system/info`, {
        headers: { 'x-api-key': candidate },
        signal: controller.signal,
      })
      // Sign-out or any other credential transition invalidates this result.
      if (controller.signal.aborted || authRevision !== getApiKeyRevision()) return
      if (!response.ok) {
        setState({
          kind: 'error',
          message:
            response.status === 401
              ? 'Key was rejected — the stored key is unchanged.'
              : `Validation failed (${response.status}) — the stored key is unchanged.`,
        })
        return
      }
      setApiKey(candidate)
      setDraft('')
      setState({ kind: 'saved' })
    } catch {
      if (controller.signal.aborted || authRevision !== getApiKeyRevision()) return
      setState({ kind: 'error', message: 'Unable to reach the APEX API — the stored key is unchanged.' })
    } finally {
      if (validationRef.current === controller) validationRef.current = null
    }
  }

  function handleSignOut() {
    validationRef.current?.abort()
    signOut()
  }

  return (
    <section className="glass-panel settings-section" aria-label="API key">
      <h3 className="settings-section-title">API key</h3>
      <dl className="settings-kv">
        <div className="settings-kv-row">
          <dt>Stored key (this browser)</dt>
          <dd data-testid="settings-key-mask">{storedKey ? maskKey(storedKey) : 'not stored'}</dd>
        </div>
      </dl>
      <form className="settings-key-form" onSubmit={(event) => void replaceKey(event)}>
        <label className="settings-field-label" htmlFor="settings-replace-key">
          Replace key
        </label>
        <div className="settings-key-row">
          <input
            id="settings-replace-key"
            className="field-input"
            type="password"
            autoComplete="off"
            placeholder="apex_…"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            disabled={validating}
          />
          <button type="submit" className="btn btn-primary btn-sm" disabled={!canSubmit}>
            {validating ? 'Validating…' : 'Validate & save'}
          </button>
        </div>
      </form>
      {state.kind === 'saved' && (
        <p className="settings-note settings-note--success" role="status">
          Key validated and saved.
        </p>
      )}
      {state.kind === 'error' && (
        <p className="settings-note settings-note--danger" role="alert">
          {state.message}
        </p>
      )}
      <div className="settings-signout-row">
        <button type="button" className="btn btn-danger btn-sm" onClick={handleSignOut}>
          Sign out
        </button>
        <span className="settings-hint">Clears the stored key and returns to the key gate.</span>
      </div>
    </section>
  )
}

const CONNECTIVITY_LABEL = {
  ok: 'Connected',
  unknown: 'Checking…',
  degraded: 'Degraded',
  unreachable: 'Unreachable',
} as const

function ConnectionSection() {
  const { status } = useConnectivity()
  const runtime = getRuntimeConfig()
  return (
    <section className="glass-panel settings-section" aria-label="Connection">
      <h3 className="settings-section-title">Connection</h3>
      <dl className="settings-kv">
        <div className="settings-kv-row">
          <dt>APEX API origin</dt>
          <dd>{runtime.apexOrigin || 'same origin'}</dd>
        </div>
        <div className="settings-kv-row">
          <dt>LangGraph origin</dt>
          <dd>{runtime.langgraphOrigin || 'same origin'}</dd>
        </div>
        <div className="settings-kv-row">
          <dt>Status</dt>
          <dd>
            <span className={`settings-status settings-status--${status}`}>
              <span className="status-dot" data-state={status} data-testid="settings-status-dot" />
              <span className="status-text">{CONNECTIVITY_LABEL[status]}</span>
            </span>
          </dd>
        </div>
      </dl>
      <p className="settings-hint">
        Origins come from /config.json at load time; empty values proxy through the dashboard
        origin.
      </p>
    </section>
  )
}

function AboutSection() {
  const { state } = useAuth()
  const info = state.status === 'authenticated' ? state.systemInfo : null
  return (
    <section className="glass-panel settings-section" aria-label="About">
      <h3 className="settings-section-title">About</h3>
      {info ? (
        <dl className="settings-kv">
          <div className="settings-kv-row">
            <dt>System</dt>
            <dd>{info.name}</dd>
          </div>
          <div className="settings-kv-row">
            <dt>Version</dt>
            <dd>{info.version}</dd>
          </div>
          <div className="settings-kv-row">
            <dt>Environment</dt>
            <dd>{info.environment}</dd>
          </div>
          <div className="settings-kv-row">
            <dt>Signed in as</dt>
            <dd>
              {info.consumer.name} <span className="dash-context-chip">{info.consumer.role}</span>
            </dd>
          </div>
        </dl>
      ) : (
        <p className="settings-hint">System info is available once a key is validated.</p>
      )}
    </section>
  )
}

export function SettingsPage() {
  return (
    <div className="settings-page animate-enter">
      <ThemeSection />
      <ApiKeySection />
      <ConnectionSection />
      <AboutSection />
    </div>
  )
}
