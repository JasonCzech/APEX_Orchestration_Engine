import { useEffect, useState } from 'react'

import CodeMirror from '@uiw/react-codemirror'

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
}: {
  values: PromptTabValues
  editable: boolean
  appAvailable: boolean
  onChange: (field: PromptTabField, value: string) => void
}) {
  const [active, setActive] = useState<PromptTabField>('system')

  useEffect(() => {
    if (active === 'application' && !appAvailable) setActive('system')
  }, [active, appAvailable])

  const activeValue =
    active === 'application' ? (values.application ?? '') : values[active]
  const activeLabel = TABS.find((tab) => tab.field === active)?.label ?? 'Prompt'
  const activeDisabled = active === 'application' && !appAvailable

  return (
    <div className="prompt-tabs-editor">
      <div className="prompt-review-tabs" role="tablist" aria-label="Prompt Review tabs">
        {TABS.map((tab) => {
          const disabled = tab.field === 'application' && !appAvailable
          return (
            <button
              key={tab.field}
              type="button"
              role="tab"
              className="prompt-review-tab"
              aria-selected={active === tab.field}
              disabled={disabled}
              onClick={() => {
                if (!disabled) setActive(tab.field)
              }}
            >
              {tab.label}
            </button>
          )
        })}
      </div>
      {activeDisabled ? (
        <div className="dash-empty small" role="tabpanel">
          No application prompt for this run.
        </div>
      ) : (
        <div
          className={`code-viewer prompt-review-editor${editable ? ' editable' : ''}`}
          role="tabpanel"
          data-testid={`prompt-tab-editor-${active}`}
        >
          <CodeMirror
            value={activeValue}
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
      )}
    </div>
  )
}
