import {
  useEffect,
  useRef,
  type FormEventHandler,
  type ReactNode,
  type RefObject,
} from 'react'

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

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
  const panelRef = useRef<HTMLElement>(null)
  const onCloseRef = useRef(onClose)
  const closeOnEscapeRef = useRef(closeOnEscape)

  useEffect(() => {
    onCloseRef.current = onClose
    closeOnEscapeRef.current = closeOnEscape
  }, [closeOnEscape, onClose])

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const panel = panelRef.current
    if (!panel) return undefined

    const active = document.activeElement
    if (!(active instanceof HTMLElement) || !panel.contains(active)) {
      const first = getFocusable(panel)[0] ?? panel
      first.focus()
    }

    function onKeyDown(event: KeyboardEvent) {
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
      previous?.focus()
    }
  }, [])

  return (
    <div
      className={overlayClassName}
      onMouseDown={(event) => {
        if (closeOnBackdrop && event.target === event.currentTarget) onClose()
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
    </div>
  )
}

function trapTab(event: KeyboardEvent, panel: HTMLElement) {
  const focusable = getFocusable(panel)
  if (focusable.length === 0) {
    event.preventDefault()
    panel.focus()
    return
  }

  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  const active = document.activeElement

  if (event.shiftKey && active === first) {
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
