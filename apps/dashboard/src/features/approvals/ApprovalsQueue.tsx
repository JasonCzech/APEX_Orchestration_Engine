import { useTimestampPreference } from '@/theme/preferences'
/**
 * Needs review queue pane (left, 360px): one row per pending gate, oldest first.
 * Pure presentation — selection/keyboard state lives in ApprovalsInboxPage.
 */
import { useEffect } from 'react'

import { formatTimestamp, gateLabel, isPhaseName, PHASE_LABELS } from '@/features/runs/runDisplay'

import type { ApprovalItem } from './useApprovalsInbox'

/** Why a row is grayed out: resumed in this pane vs cleared by another actor. */
export type RowRemovalReason = 'local' | 'elsewhere'

export interface QueueRow extends ApprovalItem {
  /** Set when the gate is no longer pending — row stays grayed until the next poll. */
  reopening?: boolean
  removed?: RowRemovalReason
}

/** Chip tone per gate kind: accent = prompt_review, info = phase_review. */
export function gateKindTone(kind: string | null | undefined): 'accent' | 'info' {
  return kind === 'prompt_review' ? 'accent' : 'info'
}

export function rowDomId(threadId: string): string {
  return `approvals-row-${threadId}`
}

export function ApprovalsQueue({
  rows,
  selectedThreadId,
  onSelect,
  focusSelection = true,
}: {
  rows: QueueRow[]
  focusSelection?: boolean
  selectedThreadId: string | null
  onSelect: (row: QueueRow) => void
}) {
  useTimestampPreference()
  useEffect(() => {
    if (!focusSelection) return
    const selected = selectedThreadId ? document.getElementById(rowDomId(selectedThreadId)) : null
    selected?.focus({ preventScroll: true })
    selected?.scrollIntoView?.({ block: 'nearest' })
  }, [selectedThreadId, focusSelection])
  return (
    <ul
      className="approvals-queue"
      role="listbox"
      aria-label="Needs review queue"
    >
      {rows.map((row, index) => {
        const selected = row.thread_id === selectedThreadId
        const classes = [
          'approvals-row',
          selected ? 'selected' : '',
          row.removed ? 'removed' : '',
        ]
          .filter(Boolean)
          .join(' ')
        return (
          <li
            key={row.thread_id}
            id={rowDomId(row.thread_id)}
            data-testid={rowDomId(row.thread_id)}
            className={classes}
            role="option"
            aria-label={`${row.title}. ${gateLabel(row.pending_gate.kind)}. ${isPhaseName(row.pending_gate.phase) ? PHASE_LABELS[row.pending_gate.phase] : 'Phase not recorded'}. Project ${row.project_id ?? 'not recorded'}. Run ${row.thread_id}. Updated ${row.age}.` }
            aria-posinset={index + 1}
            aria-setsize={rows.length}
            data-route-focus={selected && focusSelection ? true : undefined}
            aria-selected={selected}
            tabIndex={selected ? 0 : -1}
            onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); if (!row.removed) onSelect(row) } }}
            aria-disabled={row.removed ? true : undefined}
            onClick={() => {
              if (!row.removed) onSelect(row)
            }}
          >
            <div className="approvals-row-top">
              <span className="approvals-row-title" title={row.title}>
                {row.title}
              </span>
              <span
                className={`approvals-age${row.isStale && !row.removed ? ' stale' : ''}`}
                title={row.updated_at ?? undefined}
              >
                {row.age}
              </span>
            </div>
            <p className="approval-identity"><code>{row.thread_id}</code><br />Updated {formatTimestamp(row.updated_at)}</p>
            <div className="approvals-row-meta">
              {row.reopening ? <span className="topbar-meta-chip">Agent working — review will reopen</span> : row.removed ? (
                <span className="topbar-meta-chip approvals-removed-chip">
                  {row.removed === 'elsewhere' ? 'actioned elsewhere' : 'actioned'}
                </span>
              ) : (
                <span className={`topbar-meta-chip ${gateKindTone(row.pending_gate.kind)}`}>
                  {gateLabel(row.pending_gate.kind)}
                </span>
              )}
              <span className="approvals-row-phase">{isPhaseName(row.pending_gate.phase) ? PHASE_LABELS[row.pending_gate.phase] : 'Phase not recorded'}</span>
              {row.project_id && <span className="approvals-row-project">{row.project_id}</span>}
            </div>
          </li>
        )
      })}
    </ul>
  )
}
