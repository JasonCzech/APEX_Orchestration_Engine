import { useEffect, useRef, useState } from 'react'
import { gateLabel, isPhaseName, PHASE_LABELS, statusLabel } from './runDisplay'
import { liveStatusLabel } from './liveStatusDisplay'

interface AnnouncedSnapshot {
  threadId: string
  title: string
  phases: Map<string, string>
  currentPhase: string | null
  gateKey: string
  gateId: string | null
  gateText: string
  transport: string
  position: string
}

function phaseLabel(phase: string) {
  return isPhaseName(phase) ? PHASE_LABELS[phase] : phase
}

/** Selection is brief; later updates announce only changed facts, never tokens or ticks. */
export function RunAnnouncements({ threadId, title, phases, currentPhase, gate, gateGeneration, streamStatus, terminal, triage = false, queuePosition }: {
  threadId: string
  title: string
  phases: readonly { phase: string; status: string; attempt?: number | null }[]
  currentPhase?: string | null
  gate?: { phase: string; kind: string; interrupt_id?: string | null } | null
  /** Canonical server timestamp/payload generation; never the client poll time. */
  gateGeneration?: string
  streamStatus: string
  terminal: boolean
  triage?: boolean
  queuePosition?: { position: number; total: number }
}) {
  const previous = useRef<AnnouncedSnapshot | null>(null)
  const [announcement, setAnnouncement] = useState({ text: '', revision: 0 })
  useEffect(() => {
    const statuses = new Map(phases.map(phase => [phase.phase, `${statusLabel(phase.status)}${phase.attempt ? ` (attempt ${phase.attempt})` : ''}`]))
    const gateId = gate?.interrupt_id ?? null
    const gateKey = gate ? JSON.stringify([gate.kind, gate.phase, gateId, gateGeneration ?? null]) : ''
    const gateText = gate ? `${gateLabel(gate.kind)} open on ${phaseLabel(gate.phase)}.` : 'No open review.'
    const transport = terminal ? 'Recorded snapshot.' : `${liveStatusLabel(streamStatus)}.`
    const position = queuePosition ? `Queue item ${queuePosition.position} of ${queuePosition.total}.` : ''
    const current = currentPhase ?? gate?.phase ?? null
    const phaseText = current && statuses.has(current) ? `${phaseLabel(current)}: ${statuses.get(current)}.` : ''
    const before = previous.current
    previous.current = { threadId, title, phases: statuses, currentPhase: current, gateKey, gateId, gateText, transport, position }

    if (!before || before.threadId !== threadId) {
      const text = [`${title}.`, gateText, gate?.phase === current ? '' : phaseText, position, triage ? '' : transport].filter(Boolean).join(' ')
      setAnnouncement(previous => ({ text, revision: previous.revision + 1 }))
      return
    }
    const changes: string[] = []
    if (gateKey !== before.gateKey) changes.push(gateText === before.gateText
      ? gateText.replace(' open on ', gateId === before.gateId ? ' updated on ' : ' reopened on ') : gateText)
    for (const phase of phases) {
      if (statuses.get(phase.phase) !== before.phases.get(phase.phase) || (phase.phase === current && current !== before.currentPhase)) {
        changes.push(`${phaseLabel(phase.phase)}: ${statuses.get(phase.phase)}.`)
      }
    }
    if (transport !== before.transport) changes.push(transport)
    if (position && position !== before.position) changes.push(position)
    if (changes.length || title !== before.title) {
      const text = [`${title}.`, ...changes].join(' ')
      setAnnouncement(previous => ({ text, revision: previous.revision + 1 }))
    }
  }, [threadId, title, phases, currentPhase, gate, gateGeneration, streamStatus, terminal, triage, queuePosition])
  // New interrupts can need the same words. Replace only the text child so
  // assistive technology receives a real mutation without replacing its region.
  return <div className="sr-only" role="status" aria-label="Run updates" aria-live="polite" aria-atomic="true"><span key={announcement.revision}>{announcement.text}</span></div>
}
