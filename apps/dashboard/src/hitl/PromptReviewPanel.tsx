/**
 * prompt_review gate panel (plan 2.a): editable system/user/application prompt editors
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
    <div className="gate-panel prompt-review-layout" data-testid="prompt-review-panel">
      <div className="prompt-review-main">
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
        {payload.prompt.application !== null && payload.prompt.application !== undefined && (
          <PromptEditor
            field="application"
            label="Application prompt"
            value={draft.application ?? ''}
            editable={editable}
            onChange={(patch) => onEdit({ prompt: patch })}
          />
        )}
      </div>

      <div className="prompt-review-sidebar">
        <section className="gate-info-card">
          <h4 className="gate-field-label">Phase Summary</h4>
          <p className="gate-info-copy">
            Review the resolved prompt, confirm the supporting context, then execute this phase.
          </p>
          <p className="gate-info-copy">
            Prompt source: <strong>{source.origin ?? 'runtime'}</strong>
            {source.ref ? ` · ${source.ref}` : ''}
          </p>
        </section>

        {payload.context_packets.length > 0 && (
          <section className="gate-info-card" data-testid="gate-context-packets">
            <h4 className="gate-field-label">Additional Context</h4>
            <ul className="gate-packet-list">
              {payload.context_packets.map((packet, index) => (
                <li key={packet.id ?? index} className="gate-packet">
                  <span className="gate-packet-title">{packet.title ?? packet.id ?? 'packet'}</span>
                  {packet.source && <span className="kind-chip">{packet.source}</span>}
                  {packet.summary && <span className="gate-packet-summary">{packet.summary}</span>}
                </li>
              ))}
            </ul>
          </section>
        )}

        {payload.tools.length > 0 && (
          <section className="gate-info-card gate-tools" data-testid="gate-tools">
            <h4 className="gate-field-label">Tools in Play</h4>
            <div className="gate-chip-row">
              {payload.tools.map((tool) => (
                <span key={tool} className="kind-chip">
                  {tool}
                </span>
              ))}
            </div>
          </section>
        )}

        {!compact && (
          <section className="gate-info-card">
            <h4 className="gate-field-label">Post-Result Notes</h4>
            <p className="gate-info-copy">
              Result notes will appear after execution completes and the next review gate opens.
            </p>
          </section>
        )}
      </div>
    </div>
  )
}
