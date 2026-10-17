import { useEffect, useState } from 'react'
import { gateLabel, isPhaseName, PHASE_LABELS, statusLabel } from './runDisplay'
import { liveStatusLabel } from './liveStatusDisplay'

/** One atomic announcement for meaningful snapshot/transport changes, never tokens or ticks. */
export function RunAnnouncements({ title, phases, gate, streamStatus, terminal }: {
  title: string
  phases: readonly { phase: string; status: string }[]
  gate?: { phase: string; kind: string } | null
  streamStatus: string
  terminal: boolean
}) {
  const phaseChanges = phases.filter(phase => phase.status !== 'none').map(phase => `${isPhaseName(phase.phase) ? PHASE_LABELS[phase.phase] : phase.phase}: ${statusLabel(phase.status)}`).join('. ')
  const gateText = gate ? `${gateLabel(gate.kind)} open on ${isPhaseName(gate.phase) ? PHASE_LABELS[gate.phase] : gate.phase}.` : 'No open review.'
  const text = `${title}. ${gateText} ${phaseChanges}. ${terminal ? 'Recorded snapshot.' : liveStatusLabel(streamStatus)}`
  const [announcement, setAnnouncement] = useState('')
  useEffect(() => setAnnouncement(text), [text])
  return <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">{announcement}</div>
}
