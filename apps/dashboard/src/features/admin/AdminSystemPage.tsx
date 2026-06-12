/**
 * /admin/system — read-only operational summary (plan Part 2 route table + UX
 * 2.f, D7). Admin-gated.
 *
 * Cards: system info (name/version/environment), your identity (the consumer
 * behind the current key), feature flags, connectivity (ConnectivityContext
 * status + the system/info query's last fetch time) and quick links.
 */
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'

import { fetchSystemInfo } from '@/api/apexClient'
import { queryKeys, STALE_TIMES } from '@/api/queryKeys'
import { useAuth } from '@/auth/AuthProvider'
import { useConnectivity } from '@/health/ConnectivityProvider'
import { formatRelative } from '@/utils/time'

import { scopeLabel } from './adminLogic'
import { AdminGate } from './adminShared'
import './admin.css'

const EM_DASH = '—'

const STATUS_BADGE: Record<string, string> = {
  ok: 'success',
  degraded: 'warning',
  unreachable: 'danger',
  unknown: 'neutral',
}

function AdminSystemContent() {
  const { state } = useAuth()
  const { status } = useConnectivity()
  // Same key as the ConnectivityProvider's 30s poll — this subscription dedupes
  // with it and exposes dataUpdatedAt for the "last checked" caption.
  const info = useQuery({
    queryKey: queryKeys.system.info(),
    queryFn: fetchSystemInfo,
    staleTime: STALE_TIMES.systemInfo,
  })

  const systemInfo = info.data ?? (state.status === 'authenticated' ? state.systemInfo : null)
  const consumer = state.status === 'authenticated' ? state.consumer : null
  const features = Object.entries(systemInfo?.features ?? {})

  return (
    <section className="adm-page animate-enter">
      <div className="adm-system-grid">
        <div className="adm-card-panel glass-panel" aria-label="System info">
          <h2 className="adm-panel-title">System</h2>
          <dl className="adm-info-grid">
            <dt>Name</dt>
            <dd>{systemInfo?.name ?? EM_DASH}</dd>
            <dt>Version</dt>
            <dd>
              <code className="adm-fingerprint">{systemInfo?.version ?? EM_DASH}</code>
            </dd>
            <dt>Environment</dt>
            <dd>
              {systemInfo ? (
                <span className="dash-context-chip">{systemInfo.environment}</span>
              ) : (
                EM_DASH
              )}
            </dd>
          </dl>
        </div>

        <div className="adm-card-panel glass-panel" aria-label="Your identity">
          <h2 className="adm-panel-title">Your identity</h2>
          <dl className="adm-info-grid">
            <dt>Consumer</dt>
            <dd>{consumer?.name ?? EM_DASH}</dd>
            <dt>Role</dt>
            <dd>
              {consumer ? <span className="status-badge accent">{consumer.role}</span> : EM_DASH}
            </dd>
            <dt>Scopes</dt>
            <dd>
              {!consumer || consumer.scopes.length === 0 ? (
                <span className="adm-muted">{EM_DASH}</span>
              ) : (
                <ul className="adm-scope-list">
                  {consumer.scopes.map((scope) => (
                    <li key={scopeLabel(scope)}>
                      <code className="adm-fingerprint">{scopeLabel(scope)}</code>
                    </li>
                  ))}
                </ul>
              )}
            </dd>
          </dl>
        </div>

        <div className="adm-card-panel glass-panel" aria-label="Features">
          <h2 className="adm-panel-title">Features</h2>
          {features.length === 0 ? (
            <p className="adm-muted">none</p>
          ) : (
            <ul className="adm-feature-list">
              {features.map(([flag, enabled]) => (
                <li key={flag} className="adm-feature-row">
                  <code className="adm-fingerprint">{flag}</code>
                  <span className={`status-badge ${enabled ? 'success' : 'neutral'}`}>
                    {enabled ? 'on' : 'off'}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="adm-card-panel glass-panel" aria-label="Connectivity">
          <h2 className="adm-panel-title">Connectivity</h2>
          <dl className="adm-info-grid">
            <dt>Status</dt>
            <dd>
              <span className={`status-badge ${STATUS_BADGE[status] ?? 'neutral'}`}>{status}</span>
            </dd>
            <dt>Last checked</dt>
            <dd>
              {info.dataUpdatedAt > 0
                ? formatRelative(new Date(info.dataUpdatedAt).toISOString())
                : EM_DASH}
            </dd>
          </dl>
        </div>

        <div className="adm-card-panel glass-panel" aria-label="Quick links">
          <h2 className="adm-panel-title">Quick links</h2>
          <ul className="adm-link-list">
            <li>
              <Link to="/admin/connections">Connection registry</Link>
            </li>
            <li>
              <Link to="/admin/consumers">API consumers</Link>
            </li>
          </ul>
          <p className="adm-muted">Operational runbooks: see docs/runbooks in the repo.</p>
        </div>
      </div>
    </section>
  )
}

export function AdminSystemPage() {
  return (
    <AdminGate>
      <AdminSystemContent />
    </AdminGate>
  )
}
