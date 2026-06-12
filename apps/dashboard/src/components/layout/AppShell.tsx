import { Outlet } from 'react-router'

import { Sidebar } from './Sidebar'
import { Topbar } from './Topbar'
import './AppShell.css'

/**
 * APEX Load shell: collapsible sidebar + main column with sticky topbar.
 * Shell layout classes are ported from APEX Load's App.css (AppShell.css);
 * tokens and primitives come from the ported token sheet (src/theme).
 */
export function AppShell() {
  return (
    <div className="app-layout">
      <Sidebar />
      <main className="main-content">
        <Topbar />
        <Outlet />
      </main>
    </div>
  )
}
