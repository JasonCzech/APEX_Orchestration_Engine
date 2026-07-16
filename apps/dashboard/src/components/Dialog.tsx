import {
  useEffect,
  useRef,
  type FormEventHandler,
  type ReactNode,
  type RefObject,
} from 'react'
import { createPortal } from 'react-dom'

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

const DIALOG_PORTAL_ID = 'apex-dialog-portal'

interface InertSnapshot {
  element: HTMLElement
  previousInert: boolean
  previousAriaHidden: string | null
}

interface ActiveDialog extends InertSnapshot {
  token: symbol
}

const activeDialogs: ActiveDialog[] = []
let bodySnapshots: InertSnapshot[] = []
let previousBodyOverflow: string | null = null
let bodyObserver: MutationObserver | null = null

export interface DialogProps {
  children: ReactNode
  onClose: () => void
  className: string
  overlayClassName: string
  ariaLabel?: string
  labelledBy?: string
  closeOnBackdrop?: boolean
  closeOnEscape?: boolean
  panelAs?: 'div' | 'form'
  onSubmit?: FormEventHandler<HTMLFormElement>
}

export function Dialog({
  children,
  onClose,
  className,
  overlayClassName,
  ariaLabel,
  labelledBy,
  closeOnBackdrop = true,
  closeOnEscape = true,
  panelAs = 'div',
  onSubmit,
}: DialogProps) {
  const overlayRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLElement>(null)
  const dialogTokenRef = useRef(Symbol('dialog'))
  const onCloseRef = useRef(onClose)
  const closeOnEscapeRef = useRef(closeOnEscape)
  const portalHost = getDialogPortalHost()

  useEffect(() => {
    onCloseRef.current = onClose
    closeOnEscapeRef.current = closeOnEscape
  }, [closeOnEscape, onClose])

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const panel = panelRef.current
    const overlay = overlayRef.current
    if (!panel || !overlay) return undefined
    const token = dialogTokenRef.current
    const releaseDocumentModal = registerDocumentModal(portalHost, overlay, token)

    const active = document.activeElement
    if (!(active instanceof HTMLElement) || !panel.contains(active)) {
      const first = getFocusable(panel)[0] ?? panel
      first.focus()
    }

    function onKeyDown(event: KeyboardEvent) {
      if (!isTopDialog(token)) return
      if (event.key === 'Escape' && closeOnEscapeRef.current) {
        event.preventDefault()
        onCloseRef.current()
        return
      }
      if (event.key !== 'Tab' || !panel) return
      trapTab(event, panel)
    }

    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      releaseDocumentModal()
      if (previous && !previous.closest('[inert]')) previous.focus()
    }
  }, [portalHost])

  return createPortal(
    <div
      ref={overlayRef}
      className={overlayClassName}
      style={{ pointerEvents: 'auto' }}
      onMouseDown={(event) => {
        if (!isTopDialog(dialogTokenRef.current)) return
        if (event.target !== event.currentTarget) return
        if (closeOnBackdrop) {
          onClose()
          return
        }
        focusFirst(panelRef.current)
      }}
    >
      {panelAs === 'form' ? (
        <form
          ref={panelRef as RefObject<HTMLFormElement>}
          className={className}
          role="dialog"
          aria-modal="true"
          aria-label={ariaLabel}
          aria-labelledby={labelledBy}
          tabIndex={-1}
          onSubmit={onSubmit}
        >
          {children}
        </form>
      ) : (
        <div
          ref={panelRef as RefObject<HTMLDivElement>}
          className={className}
          role="dialog"
          aria-modal="true"
          aria-label={ariaLabel}
          aria-labelledby={labelledBy}
          tabIndex={-1}
        >
          {children}
        </div>
      )}
    </div>,
    portalHost,
  )
}

function trapTab(event: KeyboardEvent, panel: HTMLElement) {
  const focusable = getFocusable(panel)
  if (focusable.length === 0) {
    event.preventDefault()
    panel.focus()
    return
  }

  const first = focusable[0]!
  const last = focusable[focusable.length - 1]!
  const active = document.activeElement

  if (!(active instanceof HTMLElement) || !panel.contains(active)) {
    event.preventDefault()
    const target = event.shiftKey ? last : first
    target.focus()
  } else if (event.shiftKey && active === first) {
    event.preventDefault()
    last?.focus()
  } else if (!event.shiftKey && active === last) {
    event.preventDefault()
    first?.focus()
  }
}

function getFocusable(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) =>
      element.tabIndex >= 0 &&
      !element.hasAttribute('disabled') &&
      element.getAttribute('aria-hidden') !== 'true',
  )
}

function focusFirst(panel: HTMLElement | null) {
  if (!panel) return
  const first = getFocusable(panel)[0] ?? panel
  first.focus()
}

function getDialogPortalHost(): HTMLElement {
  const existing = document.getElementById(DIALOG_PORTAL_ID)
  if (existing instanceof HTMLElement) return existing

  const host = document.createElement('div')
  host.id = DIALOG_PORTAL_ID
  host.style.position = 'fixed'
  host.style.inset = '0'
  host.style.zIndex = '1000'
  host.style.pointerEvents = 'none'
  document.body.append(host)
  return host
}

function registerDocumentModal(host: HTMLElement, overlay: HTMLElement, token: symbol) {
  if (activeDialogs.length === 0) {
    previousBodyOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    bodySnapshots = Array.from(document.body.children)
      .filter(
        (element): element is HTMLElement =>
          element instanceof HTMLElement && element !== host,
      )
      .map(snapshotAndInert)

    bodyObserver = new MutationObserver((records) => {
      for (const record of records) {
        for (const node of record.addedNodes) {
          if (
            node instanceof HTMLElement &&
            node.parentElement === document.body &&
            node !== host &&
            !bodySnapshots.some(({ element }) => element === node)
          ) {
            bodySnapshots.push(snapshotAndInert(node))
          }
        }
      }
    })
    bodyObserver.observe(document.body, { childList: true })
  }

  activeDialogs.push({
    token,
    element: overlay,
    previousInert: overlay.inert,
    previousAriaHidden: overlay.getAttribute('aria-hidden'),
  })
  syncDialogStack()

  return () => {
    const index = activeDialogs.findIndex((dialog) => dialog.token === token)
    if (index === -1) return
    const [removed] = activeDialogs.splice(index, 1)
    if (removed) restoreInert(removed)
    syncDialogStack()

    if (activeDialogs.length === 0) {
      bodyObserver?.disconnect()
      bodyObserver = null
      for (const snapshot of bodySnapshots) restoreInert(snapshot)
      bodySnapshots = []
      if (previousBodyOverflow !== null) {
        document.body.style.overflow = previousBodyOverflow
        previousBodyOverflow = null
      }
    }
  }
}

function isTopDialog(token: symbol): boolean {
  return activeDialogs.at(-1)?.token === token
}

function syncDialogStack() {
  const topIndex = activeDialogs.length - 1
  activeDialogs.forEach((dialog, index) => {
    if (index === topIndex) {
      restoreInert(dialog)
    } else {
      dialog.element.inert = true
      dialog.element.setAttribute('aria-hidden', 'true')
    }
  })
}

function snapshotAndInert(element: HTMLElement): InertSnapshot {
  const snapshot = {
    element,
    previousInert: element.inert,
    previousAriaHidden: element.getAttribute('aria-hidden'),
  }
  element.inert = true
  element.setAttribute('aria-hidden', 'true')
  return snapshot
}

function restoreInert({
  element,
  previousInert,
  previousAriaHidden,
}: InertSnapshot) {
  element.inert = previousInert
  if (previousAriaHidden === null) {
    element.removeAttribute('aria-hidden')
  } else {
    element.setAttribute('aria-hidden', previousAriaHidden)
  }
}
