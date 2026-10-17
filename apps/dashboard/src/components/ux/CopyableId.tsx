import { useState } from 'react'
import './copyable-id.css'

/** Full, selectable identity stays in the DOM even when the visual chip is short. */
export function CopyableId({ value, label = 'ID', truncate = true }: { value: string; label?: string; truncate?: boolean }) {
  const [feedback, setFeedback] = useState<{ value: string; message: string; failed?: boolean }>()
  const current = feedback?.value === value ? feedback : undefined
  async function copy() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable')
      await navigator.clipboard.writeText(value)
      setFeedback({ value, message: `${label} copied` })
    } catch {
      setFeedback({ value, message: `Clipboard unavailable. Select and copy the full ${label} shown here.`, failed: true })
    }
  }
  return <span className="ux-copyable-id">
    <code className={truncate && !current?.failed ? 'ux-id-truncated' : undefined} title={value}>{value}</code>
    <button type="button" className="btn btn-ghost btn-sm" aria-label={`Copy ${label}`} onClick={() => void copy()}>Copy</button>
    <span role="status" className={current?.failed ? 'ux-copy-error' : 'sr-only'}>{current?.message}</span>
  </span>
}
