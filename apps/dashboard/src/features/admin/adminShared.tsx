/**
 * Shared admin bits (D7): the page-level role gate and small presentational
 * pieces (kind/provider chips, enabled toggle pill) reused across screens.
 *
 * The Sidebar already hides the Admin section from non-admins; AdminGate is
 * the in-route guard for deep links. The server enforces regardless.
 */
import type { ReactNode } from 'react'

import { RequireRole } from '@/auth/RequireRole'

/** dash-empty fallback for non-admin consumers (RequireRole pattern). */
export function AdminGate({ children }: { children: ReactNode }) {
  return (
    <RequireRole
      role="admin"
      fallback={
        <div className="dash-empty">
          <h2>Requires admin role</h2>
          <p className="dash-empty-hint">
            Your consumer does not have the admin role. Ask an administrator for access.
          </p>
        </div>
      }
    >
      {children}
    </RequireRole>
  )
}

/** Enabled/disabled toggle pill — aria-pressed carries the state. */
export function TogglePill({
  enabled,
  label,
  pending = false,
  onToggle,
}: {
  enabled: boolean
  label: string
  pending?: boolean
  onToggle: () => void
}) {
  return (
    <button
      type="button"
      className={`adm-toggle${enabled ? ' on' : ''}`}
      aria-pressed={enabled}
      aria-label={label}
      disabled={pending}
      onClick={(event) => {
        // Cards/rows behind the pill navigate on click — keep that separate.
        event.stopPropagation()
        onToggle()
      }}
    >
      <span className="adm-toggle-knob" aria-hidden="true" />
      {enabled ? 'enabled' : 'disabled'}
    </button>
  )
}
