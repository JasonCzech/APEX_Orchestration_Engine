import { useEffect, useRef, useState } from 'react'
import { Outlet, useLocation, useNavigationType } from 'react-router'
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
  const navigationType = useNavigationType()
  const mainRef = useRef<HTMLElement>(null)
  const previousLocation = useRef({ key: location.key, pathname: location.pathname })
  const scrollPositions = useRef(new Map<string, number>())
  useEffect(() => {
    const previous = previousLocation.current
    const main = mainRef.current
    if (previous.key !== location.key && main) {
      scrollPositions.current.set(previous.key, main.scrollTop)
    }
    previousLocation.current = { key: location.key, pathname: location.pathname }
    if (previous.pathname === location.pathname) return
    setMenuOpen(false)
    const frame = requestAnimationFrame(() => {
      const nextMain = mainRef.current
      if (!nextMain) return
      // Each destination starts at its own top. Browser Back/Forward restores
      // the position we recorded for that history entry instead of inheriting
      // the scroll offset from the page just left.
      nextMain.scrollTop =
        navigationType === 'POP' ? (scrollPositions.current.get(location.key) ?? 0) : 0
      // Destination controls and portals may have received focus before this
      // deferred callback runs. Never steal it from an opened menu or dialog.
      const active = document.activeElement
      const destinationOwnsFocus = active instanceof HTMLElement && (
        (nextMain.contains(active) && !active.closest('.main-topbar')) || Boolean(active.closest('[role="dialog"], [role="menu"]'))
      )
      if (!destinationOwnsFocus) nextMain.focus()
    })
    return () => cancelAnimationFrame(frame)
  }, [location.key, location.pathname, navigationType])
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
