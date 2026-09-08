import { useEffect, useRef, useState } from 'react'
import { Outlet, useLocation } from 'react-router'
import { NavigationGuards } from '@/components/NavigationGuards'
import { Dialog } from '@/components/Dialog'
import { useCompactLayout } from '@/components/hooks/useCompactLayout'

import { ConsumerKeyLifecycle } from '@/features/admin/ConsumerKeyLifecycle'

import { Sidebar } from './Sidebar'
import { Topbar } from './Topbar'
import './AppShell.css'

/**
 * APEX Load shell: collapsible sidebar + main column with sticky topbar.
 * Shell layout classes are ported from APEX Load's App.css (AppShell.css);
 * tokens and primitives come from the ported token sheet (src/theme).
 */
export function AppShell() {
  return <NavigationGuards><ShellContent /></NavigationGuards>
}

function ShellContent() {
  const compact = useCompactLayout()
  const [menuOpen, setMenuOpen] = useState(false)
  const location = useLocation()
  const mainRef = useRef<HTMLElement>(null)
  const previousPath = useRef(location.pathname)
  useEffect(() => {
    if (previousPath.current === location.pathname) return
    previousPath.current = location.pathname
    setMenuOpen(false)
    const frame = requestAnimationFrame(() => mainRef.current?.focus())
    return () => cancelAnimationFrame(frame)
  }, [location.pathname])
  return (
    <div className="app-layout">
      <a className="skip-link" href="#main-content">Skip to content</a>
      {!compact && <Sidebar />}
      {compact && menuOpen && (
        <Dialog className="navigation-drawer" overlayClassName="navigation-overlay" ariaLabel="Navigation" onClose={() => setMenuOpen(false)}>
          <button type="button" className="btn btn-secondary" onClick={() => setMenuOpen(false)}>Close navigation</button>
          <Sidebar drawer onNavigate={() => setMenuOpen(false)} />
        </Dialog>
      )}
      <main id="main-content" ref={mainRef} tabIndex={-1} className="main-content">
        <Topbar navigation={compact ? <button type="button" className="btn btn-secondary navigation-trigger" aria-haspopup="dialog" aria-expanded={menuOpen} onClick={() => setMenuOpen(true)}>Open navigation</button> : undefined} />
        <Outlet />
      </main>
      <ConsumerKeyLifecycle />
    </div>
  )
}
