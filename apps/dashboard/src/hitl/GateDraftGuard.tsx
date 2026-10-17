import './gate-dialog.css'
import { useContext, useEffect } from 'react'
import { Dialog } from '@/components/Dialog'
import { NavigationGuardContext, useNavigationBlocker } from '@/components/useNavigationBlocker'

/** The shell owns the single router blocker; previews outside it still warn on closing. */
export function GateDraftGuard({ dirty }: { dirty: boolean }) {
  const guards = useContext(NavigationGuardContext)
  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])
  return guards ? <NavigationGuard dirty={dirty} /> : null
}

function NavigationGuard({ dirty }: { dirty: boolean }) {
  const blocker = useNavigationBlocker(dirty, 10)
  if (blocker.state !== 'blocked') return null
  return <Dialog className="gate-dialog glass-panel" overlayClassName="gate-dialog-overlay" ariaLabel="Unsaved review edits" onClose={() => blocker.reset()}>
    <h2>Unsaved review edits</h2>
    <p>Your changes have not been sent to the agent. Continue reviewing to save them, or leave and keep this draft for this gate during this session.</p>
    <div className="ux-actions">
      <button type="button" className="btn btn-primary" onClick={() => blocker.reset()}>Continue reviewing</button>
      <button type="button" className="btn btn-secondary" onClick={() => blocker.proceed()}>Leave and keep draft</button>
    </div>
  </Dialog>
}
