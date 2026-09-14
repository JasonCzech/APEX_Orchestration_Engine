import { useState, type ReactNode } from 'react'
import { Link, useLocation } from 'react-router'

import type { Role } from '@/api/apexClient'
import { useAuth, useConsumer } from '@/auth/AuthProvider'
import { RequireRole } from '@/auth/RequireRole'
import { useDevDataMode } from '@/dev-data'
import { isDemoDataMode } from '@/config/runtimeConfig'
import { useApprovalsInbox } from '@/features/approvals/useApprovalsInbox'
import { useConnectivity } from '@/health/ConnectivityProvider'
import { isThemeName, THEME_LABELS, useTheme } from '@/theme/useTheme'

import type { ConnectivityStatus } from '@/health/useSystemHealth'
import './Sidebar.css'

const COLLAPSED_STORAGE_KEY = 'apex.sidebarCollapsed'

interface NavItem {
  to: string
  label: string
  icon: ReactNode
}

interface NavSection {
  label: string
  requiresRole?: Role
  items: NavItem[]
}

/**
 * Live pending-gate count on the Approvals nav item (D3). Shares the inbox's
 * react-query cache entry (same hook, same key), so this adds no extra
 * polling; pulses when any gate has been waiting > 15m. Hidden while loading,
 * on error, or at zero.
 */
function ApprovalsBadge() {
  const { count, hasStale } = useApprovalsInbox()
  if (count === 0) return null
  return (
    <span
      className={hasStale ? 'dash-badge nav-badge pulse' : 'dash-badge nav-badge'}
      data-testid="approvals-nav-badge"
      aria-label={`${count} pending approval${count === 1 ? '' : 's'}`}
    >
      {count}
    </span>
  )
}

function Icon({ d }: { d: string }) {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={d} />
    </svg>
  )
}

const TESTING_NAV_ITEMS: NavItem[] = [
  {
    to: '/',
    label: 'Home',
    icon: <Icon d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z" />,
  },
  {
    to: '/runs',
    label: 'Tests',
    icon: <Icon d="M22 12h-4l-3 9L9 3l-3 9H2" />,
  },
  { to: '/approvals', label: 'Approvals', icon: <Icon d="M3 13h4l2 3h6l2-3h4M5 5h14l2 8v6H3v-6z" /> },
  { to: '/work-items', label: 'Tickets', icon: <Icon d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01" /> },
]

const RESOURCE_NAV_ITEMS: NavItem[] = [
  {
    to: '/environments',
    label: 'Applications',
    icon: <Icon d="M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01" />,
  },
  { to: '/context', label: 'Knowledge & Documents', icon: <Icon d="m12 2 10 6-10 6L2 8zM2 16l10 6 10-6" /> },
  {
    to: '/golden-configs',
    label: 'Test Presets',
    icon: <Icon d="m12 3 2.7 5.6 6.3.9-4.5 4.4 1 6.1-5.5-2.9L6.5 20l1-6.1L3 9.5l6.3-.9z" />,
  },
  {
    to: '/prompts',
    label: 'Prompts',
    icon: <Icon d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5zM20 17v4H6.5a2.5 2.5 0 0 1 0-5" />,
  },
]

const INVESTIGATION_NAV_ITEMS: NavItem[] = [
  { to: '/evidence', label: 'Evidence', icon: <Icon d="M5 3h14v18H5zM8 7h8M8 11h8M8 15h5" /> },
  { to: '/analytics', label: 'Agent Analytics', icon: <Icon d="M3 3v18h18M8 17V9m5 8V5m5 12v-6" /> },
  { to: '/logs', label: 'Logs', icon: <Icon d="M7 3h12v18H7zM3 7h4M3 12h4M3 17h4M11 8h4M11 12h4" /> },
]

const ADMIN_NAV_SECTION: NavSection = {
  label: 'Admin',
  requiresRole: 'admin',
  items: [
    { to: '/admin/connections', label: 'Connections', icon: <Icon d="M9 7V2M15 7V2M6 7h12v4a6 6 0 0 1-12 0zM12 17v5" /> },
    { to: '/admin/consumers', label: 'Consumers', icon: <Icon d="M17 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" /> },
    { to: '/admin/system', label: 'System status', icon: <Icon d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M19.1 4.9 17 7M7 17l-2.1 2.1" /> },
  ],
}

const STATUS_CLASS: Record<ConnectivityStatus, string> = {
  ok: 'connected',
  unknown: 'connecting',
  degraded: 'degraded',
  unreachable: 'disconnected',
}

const STATUS_LABEL: Record<ConnectivityStatus, string> = {
  ok: 'Connected',
  unknown: 'Checking…',
  degraded: 'Degraded',
  unreachable: 'Unreachable',
}

function initials(name: string): string {
  return (
    name
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((part) => part[0]?.toUpperCase() ?? '')
      .join('') || '?'
  )
}

function DevDataSwitch() {
  const { available, enabled, setEnabled, reset } = useDevDataMode()
  if (!available) return null

  if (isDemoDataMode()) {
    return (
      <button type="button" className="sidebar-devdata-switch on" onClick={reset}>
        <span className="sidebar-devdata-label">Reset demo</span>
      </button>
    )
  }

  return (
    <button
      type="button"
      className={enabled ? 'sidebar-devdata-switch on' : 'sidebar-devdata-switch'}
      role="switch"
      aria-checked={enabled}
      aria-label="Dummy data"
      onClick={() => setEnabled(!enabled)}
    >
      <span className="sidebar-devdata-knob" aria-hidden="true" />
      <span className="sidebar-devdata-label">Dummy data</span>
      <span className="sidebar-devdata-state">{enabled ? 'On' : 'Off'}</span>
    </button>
  )
}

function readStoredCollapsed(): boolean {
  try {
    return window.localStorage.getItem(COLLAPSED_STORAGE_KEY) === 'true'
  } catch {
    return false
  }
}

function isNavItemActive(item: NavItem, pathname: string): boolean {
  if (item.to === '/') return pathname === '/'
  if (item.to === '/runs') return pathname === '/runs' || (pathname.startsWith('/runs/') && !pathname.startsWith('/runs/new'))
  return pathname === item.to || pathname.startsWith(`${item.to}/`)
}

function SidebarLink({ item, pathname, onNavigate }: { item: NavItem; pathname: string; onNavigate?: () => void }) {
  const active = isNavItemActive(item, pathname)
  return (
    <Link
      to={item.to}
      aria-current={active ? 'page' : undefined}
      className={active ? 'nav-item active' : 'nav-item'}
      title={item.label}
      onClick={onNavigate}
    >
      <span className="nav-icon">{item.icon}</span>
      <span className="nav-label">{item.label}</span>
      {item.to === '/approvals' && <ApprovalsBadge />}
    </Link>
  )
}

function NavSection({
  section,
  pathname,
  onNavigate,
}: {
  section: NavSection
  pathname: string
  onNavigate?: () => void
}) {
  return (
    <div className="nav-section">
      <div className="nav-section-label">{section.label}</div>
      {section.items.map((item) => (
        <SidebarLink key={item.to} item={item} pathname={pathname} onNavigate={onNavigate} />
      ))}
    </div>
  )
}

export function Sidebar({ drawer = false, onNavigate }: { drawer?: boolean; onNavigate?: () => void }) {
  const [collapsed, setCollapsed] = useState(readStoredCollapsed)
  const [accountOpen, setAccountOpen] = useState(false)
  const location = useLocation()
  const consumer = useConsumer()
  const { signOut, state: authState } = useAuth()
  const { status } = useConnectivity()
  const { preference, themes, setTheme } = useTheme()
  const testingItems = [
    ...TESTING_NAV_ITEMS,
    ...(authState.status === 'authenticated' && authState.systemInfo.features.cycles
      ? [{ to: '/cycles', label: 'Test Cycles', icon: <Icon d="M4 4h16v16H4zM4 10h16M10 10v10" /> }]
      : []),
  ]

  const toggleCollapsed = () => {
    setCollapsed((prev) => {
      const next = !prev
      try {
        window.localStorage.setItem(COLLAPSED_STORAGE_KEY, String(next))
      } catch {
        // Persistence is best-effort.
      }
      return next
    })
  }

  return (
    <aside className={drawer ? 'sidebar sidebar-drawer' : collapsed ? 'sidebar collapsed' : 'sidebar'} data-testid="sidebar">
      <div className="sidebar-brand">
        <svg className="brand-icon" width="26" height="26" viewBox="0 0 24 24" aria-hidden="true">
          <defs>
            <linearGradient id="apexBrandGradient" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor="var(--accent)" />
              <stop offset="100%" stopColor="var(--info)" />
            </linearGradient>
          </defs>
          <path d="M12 2 22 12 12 22 2 12Z" fill="url(#apexBrandGradient)" opacity="0.9" />
          <path d="M12 6.5 17.5 12 12 17.5 6.5 12Z" fill="var(--bg-secondary)" />
        </svg>
        <span className="brand-name">APEX Orchestration</span>
      </div>

      <nav className="sidebar-nav" aria-label="Primary">
        <SidebarLink
          item={{ to: '/runs/new', label: 'New Test', icon: <Icon d="M12 5v14M5 12h14" /> }}
          pathname={location.pathname}
          onNavigate={onNavigate}
        />
        <NavSection section={{ label: 'Testing', items: testingItems }} pathname={location.pathname} onNavigate={onNavigate} />
        <NavSection section={{ label: 'Resources', items: RESOURCE_NAV_ITEMS }} pathname={location.pathname} onNavigate={onNavigate} />
        <NavSection section={{ label: 'Investigation', items: INVESTIGATION_NAV_ITEMS }} pathname={location.pathname} onNavigate={onNavigate} />
        <RequireRole role={ADMIN_NAV_SECTION.requiresRole as Role}>
          <NavSection section={ADMIN_NAV_SECTION} pathname={location.pathname} onNavigate={onNavigate} />
        </RequireRole>
        <div className="nav-section nav-section--settings">
          <SidebarLink
            item={{ to: '/settings', label: 'Settings', icon: <Icon d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M19.1 4.9 17 7M7 17l-2.1 2.1" /> }}
            pathname={location.pathname}
            onNavigate={onNavigate}
          />
        </div>
      </nav>

      <div className="sidebar-footer">
        <div className="sidebar-footer-meta">
          {consumer && (
            <button
              type="button"
              className="sidebar-identity"
              data-testid="sidebar-identity"
              aria-label={`Account options for ${consumer.name}`}
              aria-expanded={accountOpen}
              onClick={() => setAccountOpen((open) => !open)}
            >
              <div className="sidebar-identity-avatar" aria-hidden="true">
                {initials(consumer.name)}
              </div>
              <div className="sidebar-identity-copy">
                <span className="sidebar-identity-name">{consumer.name}</span>
                <span className="sidebar-identity-meta">{consumer.role}</span>
              </div>
            </button>
          )}
          {consumer && accountOpen && (
            <div className="sidebar-account-panel" aria-label="Account options">
              <DevDataSwitch />
              <label className="sidebar-account-theme">
                Theme
                <select
                  className="theme-select"
                  aria-label="Theme"
                  value={preference}
                  onChange={(event) => {
                    const next = event.target.value
                    if (next === 'system' || isThemeName(next)) setTheme(next)
                  }}
                >
                  <option value="system">System (Auto)</option>
                  {themes.map((name) => (
                    <option key={name} value={name}>
                      {THEME_LABELS[name]}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="sidebar-identity-action sidebar-identity-action-button"
                onClick={signOut}
              >
                Sign out
              </button>
            </div>
          )}
          <div className="sidebar-footer-row">
            <div className={`sidebar-status ${STATUS_CLASS[status]}`} title={STATUS_LABEL[status]}>
              <span
                className="status-dot"
                data-state={status}
                data-testid="connection-status-dot"
              />
              <span className="status-text">{isDemoDataMode() ? 'Offline simulation' : STATUS_LABEL[status]}</span>
            </div>
            <button
              hidden={drawer}
              type="button"
              className="collapse-btn"
              aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
              aria-pressed={collapsed}
              onClick={toggleCollapsed}
            >
              <Icon d={collapsed ? 'm9 18 6-6-6-6' : 'm15 18-6-6 6-6'} />
            </button>
          </div>
        </div>
      </div>
    </aside>
  )
}
