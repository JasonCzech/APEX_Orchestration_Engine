/**
 * Approvals queue pane (left, 360px): one row per pending gate, oldest first.
 * Pure presentation — selection/keyboard state lives in ApprovalsInboxPage.
 */
import type { ApprovalItem } from './useApprovalsInbox'

/** Why a row is grayed out: resumed in this pane vs cleared by another actor. */
export type RowRemovalReason = 'local' | 'elsewhere'

export interface QueueRow extends ApprovalItem {
  /** Set when the gate is no longer pending — row stays grayed until the next poll. */
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
}: {
  rows: QueueRow[]
  selectedThreadId: string | null
  onSelect: (row: QueueRow) => void
}) {
  return (
    <ul
      className="approvals-queue"
      role="listbox"
      aria-label="Approvals queue"
      aria-activedescendant={selectedThreadId ? rowDomId(selectedThreadId) : undefined}
    >
      {rows.map((row) => {
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
            aria-selected={selected}
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
            <div className="approvals-row-meta">
              {row.removed ? (
                <span className="topbar-meta-chip approvals-removed-chip">
                  {row.removed === 'elsewhere' ? 'actioned elsewhere' : 'actioned'}
                </span>
              ) : (
                <span className={`topbar-meta-chip ${gateKindTone(row.pending_gate.kind)}`}>
                  {row.pending_gate.kind ?? 'review'}
                </span>
              )}
              <span className="approvals-row-phase">{row.pending_gate.phase ?? '—'}</span>
              {row.project_id && <span className="approvals-row-project">{row.project_id}</span>}
            </div>
          </li>
        )
      })}
    </ul>
  )
}
