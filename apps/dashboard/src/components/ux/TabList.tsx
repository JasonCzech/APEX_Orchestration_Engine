import { useId } from 'react'

/** Shared APEX tabs: automatic activation, roving focus, and linked panels. */
export function TabList<T extends string>({
  label,
  items,
  value,
  onChange,
  id: suppliedId,
}: {
  label: string
  items: readonly { id: T; label: string }[]
  value: T
  onChange: (id: T) => void
  id?: string
}) {
  const generatedId = useId()
  const id = suppliedId ?? generatedId
  return (
    <div className="workspace-tabs" role="tablist" aria-label={label}>
      {items.map((item, index) => (
        <button
          key={item.id}
          id={`${id}-tab-${item.id}`}
          type="button"
          role="tab"
          className="workspace-tab"
          aria-selected={value === item.id}
          aria-controls={`${id}-panel-${item.id}`}
          tabIndex={value === item.id ? 0 : -1}
          onClick={() => onChange(item.id)}
          onKeyDown={(event) => {
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
