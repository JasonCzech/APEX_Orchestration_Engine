import { useEffect, useId, useState } from 'react'
import './searchable-select.css'

export interface SearchOption { value: string; label: string; description?: string }
/** Single-select combobox. Typing filters choices; only selecting commits a value. */
export function SearchableSelect({ label, value, options, onChange }: { label: string; value: string; options: SearchOption[]; onChange: (value: string) => void }) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const selected = options.find(option => option.value === value)
  const matches = options.filter(option => `${option.label} ${option.description ?? ''} ${option.value}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()))
  const activeIndex = Math.min(active, Math.max(0, matches.length - 1))
  useEffect(() => {
    if (open) document.getElementById(`${id}-option-${activeIndex}`)?.scrollIntoView?.({ block: 'nearest' })
  }, [activeIndex, id, open, query])
  function choose(option: SearchOption) { onChange(option.value); setOpen(false); setQuery('') }
  return <div className="ux-searchable-select">
    <label htmlFor={id}>{label}</label>
    <input id={id} role="combobox" aria-autocomplete="list" aria-expanded={open} aria-controls={`${id}-choices`}
      aria-activedescendant={open && matches.length ? `${id}-option-${activeIndex}` : undefined}
      aria-describedby={selected?.description ? `${id}-selected-description` : undefined}
      value={open ? query : selected?.label ?? value} placeholder="Search by title or ID"
      onFocus={() => { setOpen(true); setQuery(''); setActive(Math.max(0, options.findIndex(option => option.value === value))) }} onBlur={() => setOpen(false)}
      onClick={() => { if (!open) { setOpen(true); setQuery(''); setActive(Math.max(0, options.findIndex(option => option.value === value))) } }}
      onChange={event => { setQuery(event.target.value); setOpen(true); setActive(0) }}
      onKeyDown={event => {
        if (event.key === 'Escape') { event.preventDefault(); setOpen(false); setQuery('') }
        else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          event.preventDefault(); setOpen(true)
          setActive(!open ? event.key === 'ArrowDown' ? 0 : matches.length - 1 : (activeIndex + (event.key === 'ArrowDown' ? 1 : -1) + Math.max(1, matches.length)) % Math.max(1, matches.length))
        } else if (open && event.key === 'Enter') { event.preventDefault(); if (matches[activeIndex]) choose(matches[activeIndex]) }
      }} />
    {selected?.description && <small id={`${id}-selected-description`} className="ux-selected-description">{selected.description}</small>}
    <ul id={`${id}-choices`} role="listbox" aria-label={`${label} choices`} hidden={!open}>
      {matches.map((option, index) => <li id={`${id}-option-${index}`} key={option.value} role="option" aria-selected={value === option.value}
        className={index === activeIndex ? 'is-active' : undefined} onMouseDown={event => event.preventDefault()} onMouseMove={() => setActive(index)} onClick={() => choose(option)}>
        <span title={option.label}>{option.label}</span>{option.description && <small>{option.description}</small>}
      </li>)}
      {matches.length === 0 && <li role="presentation">No matching choices.</li>}
    </ul>
  </div>
}
