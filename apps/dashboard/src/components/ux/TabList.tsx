import { useId, type ReactNode } from 'react'

/** Shared APEX tabs: automatic activation, roving focus, and linked panels. */
export function TabList<T extends string>({
  label,
  items,
  value,
  onChange,
  id: suppliedId,
  disabled = false,
}: {
  label: string
  items: readonly { id: T; label: string }[]
  value: T
  onChange: (id: T) => void
  id?: string
  disabled?: boolean
}) {
  const generatedId = useId()
  const id = suppliedId ?? generatedId
  return (
    <div className="workspace-tabs ux-underline-tabs" role="tablist" aria-label={label}>
      {items.map((item, index) => (
        <button
          key={item.id}
          id={`${id}-tab-${item.id}`}
          type="button"
          disabled={disabled}
          role="tab"
          className="workspace-tab"
          aria-selected={value === item.id}
          aria-controls={`${id}-panel-${item.id}`}
          tabIndex={value === item.id ? 0 : -1}
          onClick={() => onChange(item.id)}
          onKeyDown={(event) => {
            if (disabled) return
            const next =
              event.key === 'ArrowRight'
                ? (index + 1) % items.length
                : event.key === 'ArrowLeft'
                  ? (index + items.length - 1) % items.length
                  : event.key === 'Home'
                    ? 0
                    : event.key === 'End'
                      ? items.length - 1
                      : -1
            if (next < 0) return
            event.preventDefault()
            const target = items[next]!
            onChange(target.id)
            document.getElementById(`${id}-tab-${target.id}`)?.focus()
          }}
        >
          {item.label}
        </button>
      ))}
    </div>
  )
}

/** Every tab owns a real panel; only the selected content remains mounted. */
export function TabPanels<T extends string>({ id, items, value, children, focusable = false }: {
  id: string; items: readonly T[]; value: T; children: ReactNode; focusable?: boolean
}) {
  return <>{items.map(item => <div key={item} id={`${id}-panel-${item}`} role="tabpanel"
    aria-labelledby={`${id}-tab-${item}`} hidden={value !== item} tabIndex={focusable && value === item ? 0 : undefined}>
    {value === item ? children : null}
  </div>)}</>
}
