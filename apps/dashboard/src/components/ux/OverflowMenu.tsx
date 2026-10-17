import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'

import './overflow-menu.css'

/** Shared menu button with keyboard navigation and focus restoration. */

export interface OverflowMenuItem {
  label: string
  onSelect: () => void
  disabled?: boolean
}

export interface OverflowMenuProps {
  /** Accessible name of the trigger button and the menu. */
  label: string
  items: OverflowMenuItem[]
  /** Trigger content; defaults to the kebab glyph. */
  trigger?: ReactNode
  className?: string
}

export function OverflowMenu({ label, items, trigger = '⋯', className = '' }: OverflowMenuProps) {
  const [open, setOpen] = useState(false)
  const menuId = useId()
  const initialFocus = useRef<'first' | 'last'>('first')
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  // The top layer escapes scrolling table containers while retaining dialog
  // ancestry, so the shared dialog's inert and focus boundaries remain valid.
  useLayoutEffect(() => {
    if (!open) return undefined
    const list = listRef.current
    const trigger = triggerRef.current
    if (!list || !trigger) return undefined
    if (typeof list.showPopover === 'function') {
      list.setAttribute('popover', 'manual')
      list.showPopover()
    }
    const position = () => {
      const anchor = trigger.getBoundingClientRect()
      const menu = list.getBoundingClientRect()
      const left = Math.max(8, Math.min(anchor.right - menu.width, window.innerWidth - menu.width - 8))
      const below = anchor.bottom + 4
      const top = below + menu.height <= window.innerHeight - 8
        ? below : Math.max(8, anchor.top - menu.height - 4)
      list.style.left = `${left}px`
      list.style.top = `${top}px`
    }
    position()
    window.addEventListener('resize', position)
    window.addEventListener('scroll', position, true)
    return () => {
      window.removeEventListener('resize', position)
      window.removeEventListener('scroll', position, true)
    }
  }, [open])

  useEffect(() => {
    if (!open) return undefined
    function onDocMouseDown(event: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onDocMouseDown)
    return () => document.removeEventListener('mousedown', onDocMouseDown)
  }, [open])

  // Focus the first item when the menu opens (menu-button pattern).
  useEffect(() => {
    if (open) {
      const nodes = listRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')
      if (nodes?.length) nodes[initialFocus.current === 'last' ? nodes.length - 1 : 0]?.focus()
      else listRef.current?.focus()
    }
  }, [open])

  function moveFocus(delta: 1 | -1) {
    const nodes = listRef.current?.querySelectorAll<HTMLButtonElement>(
      '[role="menuitem"]:not(:disabled)',
    )
    if (!nodes || nodes.length === 0) return
    const list = Array.from(nodes)
    const index = list.indexOf(document.activeElement as HTMLButtonElement)
    const next = list[index < 0 ? delta > 0 ? 0 : list.length - 1 : (index + delta + list.length) % list.length]
    next?.focus()
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape' && open) {
      event.preventDefault()
      event.stopPropagation()
      setOpen(false)
      triggerRef.current?.focus()
    } else if (event.key === 'ArrowDown') {
      event.preventDefault()
      if (open) moveFocus(1)
      else { initialFocus.current = 'first'; setOpen(true) }
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      if (open) moveFocus(-1)
      else { initialFocus.current = 'last'; setOpen(true) }
    } else if (open && (event.key === 'Home' || event.key === 'End')) {
      event.preventDefault()
      const nodes = listRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')
      if (nodes?.length) nodes[event.key === 'Home' ? 0 : nodes.length - 1]?.focus()
    }
  }

  return (
    <div
      className={`overflow-menu ${className}`.trim()}
      ref={rootRef}
      onKeyDown={onKeyDown}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false)
      }}
      // Entry point 2 mounts this inside a clickable grid row — opening the
      // menu or picking an item must never trigger the row navigation.
      onClick={(event) => event.stopPropagation()}
    >
      <button
        type="button"
        ref={triggerRef}
        className="overflow-menu-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={label}
        onClick={() => { initialFocus.current = 'first'; setOpen((value) => !value) }}
      >
        {trigger}
      </button>
      {open && (
        <div id={menuId} tabIndex={-1} className="overflow-menu-list glass-panel" role="menu" aria-label={label} ref={listRef}>
          {items.map((item) => (
            <button
              key={item.label}
              type="button"
              role="menuitem"
              tabIndex={-1}
              className="overflow-menu-item"
              disabled={item.disabled}
              onClick={() => {
                if (item.disabled) return
                setOpen(false)
                triggerRef.current?.focus()
                item.onSelect()
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
