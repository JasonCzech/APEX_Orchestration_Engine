/**
 * prompt_review gate panel (plan 2.a): editable system/user prompt editors
 * (CodeMirror — same surface family as components/viewers/CodeViewer, but
 * writable when payload.editable allows), provenance chip from
 * payload.prompt.source, collapsible context-packet list, and tool chips.
 * Edits dispatch EDIT patches into the machine draft; the dirty diff chip
 * reflects machine-computed prompt dirtiness.
 */
import CodeMirror from '@uiw/react-codemirror'

import type { PromptReviewPayload } from '@apex/pipeline-events'

import { originalPromptOf, type GateDraftPatch, type GateInstance, type PromptDraft } from './gateMachine'

function PromptEditor({
  field,
  label,
  value,
  editable,
  onChange,
}: {
  field: keyof PromptDraft
  label: string
  value: string
  editable: boolean
  onChange: (patch: Partial<PromptDraft>) => void
}) {
  return (
    <div className="gate-prompt-field">
      <h4 className="gate-field-label">{label}</h4>
      <div
        className={`code-viewer gate-editor${editable ? ' editable' : ''}`}
        data-testid={`gate-editor-${field}`}
      >
        <CodeMirror
          value={value}
          editable={editable}
          readOnly={!editable}
          basicSetup={{
            lineNumbers: true,
            foldGutter: false,
            highlightActiveLine: editable,
            highlightActiveLineGutter: false,
          }}
          onChange={(next: string) => onChange({ [field]: next })}
        />
      </div>
    </div>
  )
}

export function PromptReviewPanel({
  gate,
  payload,
  prompt,
  dirty,
  disabled,
  compact = false,
  onEdit,
}: {
  gate: GateInstance
  payload: PromptReviewPayload
  /** Draft prompt (machine draft.prompt — seeded from the payload). */
  prompt: PromptDraft | undefined
  dirty: boolean
  disabled: boolean
  compact?: boolean
  onEdit: (patch: GateDraftPatch) => void
}) {
  const editable = payload.editable && !disabled
  const draft = prompt ?? originalPromptOf(gate)
  const source = payload.prompt.source

  return (
    <div className="gate-panel" data-testid="prompt-review-panel">
      <div className="gate-chip-row">
        {source.origin && (
          <span
            className="topbar-meta-chip accent"
            data-testid="gate-provenance"
            title="Where the prompt text under review came from"
          >
            {source.origin}
            {source.ref ? ` · ${source.ref}` : ''}
          </span>
        )}
        {!payload.editable && (
          <span className="topbar-meta-chip" title="The backend marked this prompt read-only">
            read-only
          </span>
        )}
        {dirty && (
          <span
            className="topbar-meta-chip warning"
            data-testid="gate-dirty-chip"
            title="Your draft differs from the prompt the agent resolved"
          >
            edited
          </span>
        )}
      </div>

      <PromptEditor
        field="system"
        label="System prompt"
        value={draft.system}
        editable={editable}
        onChange={(patch) => onEdit({ prompt: patch })}
      />
      <PromptEditor
        field="user"
        label="User prompt"
        value={draft.user}
        editable={editable}
        onChange={(patch) => onEdit({ prompt: patch })}
      />

      {payload.tools.length > 0 && (
        <div className="gate-tools" data-testid="gate-tools">
          <h4 className="gate-field-label">Tools</h4>
          <div className="gate-chip-row">
            {payload.tools.map((tool) => (
              <span key={tool} className="kind-chip">
                {tool}
              </span>
            ))}
          </div>
        </div>
      )}

      {payload.context_packets.length > 0 && (
        <details className="gate-collapsible" data-testid="gate-context-packets" open={!compact}>
          <summary>Context packets ({payload.context_packets.length})</summary>
          <ul className="gate-packet-list">
            {payload.context_packets.map((packet, index) => (
              <li key={packet.id ?? index} className="gate-packet">
                <span className="gate-packet-title">{packet.title ?? packet.id ?? 'packet'}</span>
                {packet.source && <span className="kind-chip">{packet.source}</span>}
                {packet.summary && <span className="gate-packet-summary">{packet.summary}</span>}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}
