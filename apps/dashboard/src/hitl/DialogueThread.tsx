/**
 * Gate dialogue: the phase_review payload's dialogue_tail (last <=3 entries)
 * as operator/agent bubbles, plus the discuss composer. The composer writes
 * the machine draft's `message` (EDIT patch); the action bar's [Discuss]
 * submits it as {action: 'discuss', message}.
 */
import type { DialogueEntry } from '@apex/pipeline-events'

import { formatTimestamp } from '@/features/runs/runDisplay'

export function DialogueThread({
  entries,
  message,
  composerEnabled,
  disabled,
  onMessageChange,
}: {
  entries: DialogueEntry[]
  /** Draft message (machine draft.message). */
  message: string
  /** payload.actions includes 'discuss'. */
  composerEnabled: boolean
  /** Machine is not editable (submitting / awaiting / superseded). */
  disabled: boolean
  onMessageChange: (message: string) => void
}) {
  return (
    <div className="gate-dialogue" data-testid="gate-dialogue">
      {entries.length > 0 && (
        <div className="gate-dialogue-tail">
          {entries.map((entry) => (
            <div key={entry.id} className={`gate-dialogue-bubble ${entry.role}`}>
              <span className="gate-dialogue-role">
                {entry.role}
                {entry.at ? ` · ${formatTimestamp(entry.at)}` : ''}
              </span>
              {entry.content}
            </div>
          ))}
        </div>
      )}
      {composerEnabled && (
        <label className="gate-composer">
          <span className="gate-field-label">Discuss with the agent</span>
          <textarea
            className="field-input gate-composer-input"
            placeholder="Ask a question or give feedback — the gate reopens with the agent's reply"
            value={message}
            disabled={disabled}
            onChange={(event) => onMessageChange(event.target.value)}
          />
        </label>
      )}
    </div>
  )
}
