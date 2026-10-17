import '@/components/viewers/viewers.css'
import { useEffect, useId, useState } from 'react'
import { EditorView } from '@codemirror/view'
import { TabList } from '@/components/ux/TabList'

import CodeMirror from '@uiw/react-codemirror'
import { PLATFORM_SYSTEM_POLICY } from '../prompts/platformPolicy'

import './prompt-tabs-editor.css'

export interface PromptTabValues {
  system: string
  phase_prompt: string
  application: string | null
  additional_context: string
}

export type PromptTabField = keyof PromptTabValues

const TABS: Array<{ field: PromptTabField; label: string }> = [
  { field: 'system', label: 'System Prompt' },
  { field: 'phase_prompt', label: 'Phase Prompt' },
  { field: 'application', label: 'Application Prompt' },
  { field: 'additional_context', label: 'Additional Context' },
]

export function PromptTabsEditor({
  values,
  editable,
  appAvailable,
  onChange,
  active: activeProp,
  onActiveChange,
}: {
  values: PromptTabValues
  editable: boolean
  appAvailable: boolean
  onChange: (field: PromptTabField, value: string) => void
  /** Optional controlled active tab. Falls back to internal state (e.g. in the gate panel). */
  active?: PromptTabField
  onActiveChange?: (field: PromptTabField) => void
}) {
  const tabsId = useId()
  const [activeInternal, setActiveInternal] = useState<PromptTabField>('system')
  const active = activeProp ?? activeInternal
  const setActive = (field: PromptTabField) => {
    onActiveChange?.(field)
    if (activeProp === undefined) setActiveInternal(field)
  }

  useEffect(() => {
    if (active === 'application' && !appAvailable) setActive('system')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, appAvailable])

  const activeValue =
    active === 'application' ? (values.application ?? '') : values[active]
  const activeLabel = TABS.find((tab) => tab.field === active)?.label ?? 'Prompt'
  const activeDisabled = active === 'application' && !appAvailable

  return (
    <div className="prompt-tabs-editor">
      <details>
        <summary>Shared platform instructions (read-only)</summary>
        <pre style={{ whiteSpace: 'pre-wrap' }}>{PLATFORM_SYSTEM_POLICY}</pre>
        <p>The runtime also applies the phase output schema. Edits below cannot override these rules.</p>
      </details>
      <TabList id={tabsId} label="Prompt review tabs"
        items={TABS.filter(tab => tab.field !== 'application' || appAvailable).map(tab => ({ id: tab.field, label: tab.label }))}
        value={active} onChange={setActive} />
      {activeDisabled ? (
        <div className="dash-empty small" role="tabpanel" id={`${tabsId}-panel-${active}`} aria-labelledby={`${tabsId}-tab-${active}`}>
          No application prompt for this run.
        </div>
      ) : (
        <>
          {active === 'application' && (
            <p className="prompt-review-tab-note" data-testid="prompt-tab-note-application">
              Application-wide — saved edits apply to every phase in this run.
              {' '}Use UNKNOWN for missing requirements; include units, evidence sources and observation windows for thresholds.
            </p>
          )}
          <div
            className={`code-viewer prompt-review-editor${editable ? ' editable' : ''}`}
            role="tabpanel" id={`${tabsId}-panel-${active}`} aria-labelledby={`${tabsId}-tab-${active}`}
            data-testid={`prompt-tab-editor-${active}`}
          >
            <CodeMirror
              value={activeValue}
              indentWithTab={false}
              extensions={[EditorView.contentAttributes.of({ 'aria-label': activeLabel, tabindex: '0' })]}
              editable={editable}
              readOnly={!editable}
              aria-label={activeLabel}
              basicSetup={{
                lineNumbers: true,
                foldGutter: false,
                highlightActiveLine: editable,
                highlightActiveLineGutter: false,
              }}
              onChange={(next: string) => onChange(active, next)}
            />
          </div>
        </>
      )}
    </div>
  )
}
