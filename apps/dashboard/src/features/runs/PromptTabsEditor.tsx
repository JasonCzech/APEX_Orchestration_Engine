import { promptContextEdits, type ContextEdit } from '../prompts/promptContextEdits'
import { StateCard } from '@/components/ux/StateCard'
import '@/components/viewers/viewers.css'
import { useEffect, useId, useState } from 'react'
import { EditorView } from '@codemirror/view'
import { TabList, TabPanels } from '@/components/ux/TabList'

import CodeMirror from '@uiw/react-codemirror'
import { PLATFORM_SYSTEM_POLICY } from '../prompts/platformPolicy'

import './prompt-tabs-editor.css'

export interface PromptTabValues {
  system: string
  phase_prompt: string
  application: string | null
  additional_context: string
  context_edits?: ContextEdit[]
}

export type PromptTabField = Exclude<keyof PromptTabValues, 'context_edits'> | 'environment'
export interface PromptContextEntry { namespace: string; key: string; prompt_id: string; version: number; content: string; overridden?: boolean }

const PROMPT_CONTEXT_LABELS: Record<string, string> = { application: 'Application context', environment: 'Environment Context' }

const TABS: Array<{ field: PromptTabField; label: string }> = [
  { field: 'system', label: 'System Prompt' },
  { field: 'phase_prompt', label: 'Phase context' },
  { field: 'application', label: 'Application context' },
  { field: 'environment', label: 'Environment Context' },
  { field: 'additional_context', label: 'Additional context' },
]

export function PromptTabsEditor({
  values,
  systemLocked = false,
  contexts,
  selections,
  editable,
  appAvailable,
  onChange,
  onContextChange,
  active: activeProp,
  onActiveChange,
}: {
  values: PromptTabValues
  systemLocked?: boolean
  contexts?: PromptContextEntry[]
  selections?: Array<Record<string, unknown>>
  editable: boolean
  appAvailable: boolean
  onChange: (field: PromptTabField, value: string) => void
  onContextChange?: (edits: ContextEdit[]) => void
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

  const hasApplicationContext = contexts ? contexts.some(item => item.namespace === 'application') : appAvailable
  useEffect(() => {
    if (active === 'application' && !hasApplicationContext) setActive('system')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, hasApplicationContext])

  const activeValue =
    active === 'environment' ? '' : active === 'application' ? (values.application ?? '') : values[active]
  const activeLabel = systemLocked ? TABS.find((tab) => tab.field === active)?.label ?? 'Prompt' : ({ system: 'System prompt', phase_prompt: 'Phase prompt', application: 'Application prompt', environment: 'Environment Context', additional_context: 'Additional context' }[active])
  const activeDisabled = active === 'application' && !hasApplicationContext
  const selectedEntry = selections?.find(item => item.namespace === (active === 'phase_prompt' ? 'phase' : active))
  const canEdit = editable && !(systemLocked && active === 'system')
  const legacyLabels: Record<string, string> = { system: 'System prompt', phase_prompt: 'Phase prompt', application: 'Application prompt' }
  const visibleTabs = TABS.map(tab => ({ ...tab, label: !systemLocked ? legacyLabels[tab.field] ?? tab.label : tab.label })).filter(tab => tab.field === 'environment' ? contexts?.some(item => item.namespace === 'environment') : tab.field === 'application' ? (contexts ? contexts.some(item => item.namespace === 'application') : appAvailable) : true)
  function contextContent(item: PromptContextEntry): string {
    return values.context_edits?.find(edit => edit.prompt_id === item.prompt_id)?.content ?? item.content
  }

  function editContext(index: number, content: string) {
    const edits = promptContextEdits(contexts ?? []).map((item, i) => ({
      ...item, content: i === index ? content : contextContent(contexts![i]!),
    }))
    onContextChange?.(edits)
  }


  return (
    <div className="prompt-tabs-editor">
      <details>
        <summary>Shared platform instructions (read-only)</summary>
        <pre style={{ whiteSpace: 'pre-wrap' }}>{PLATFORM_SYSTEM_POLICY}</pre>
        <p>The runtime also applies the phase output schema. Edits below cannot override these rules.</p>
      </details>
      <TabList id={tabsId} label="Prompt review tabs"
        items={visibleTabs.map(tab => ({ id: tab.field, label: tab.label }))}
        value={active} onChange={setActive} />
      <TabPanels id={tabsId} items={visibleTabs.map(tab => tab.field)} value={active}>
      {contexts && (active === 'application' || active === 'environment') ? <div>
        <p>Edits apply only to this phase. Selected prompt membership is fixed for this test.</p>
        {contexts.map((item, index) => item.namespace === active ? <section key={item.prompt_id}><h4>{item.key} · v{item.version}{item.overridden ? ' · setup override' : ''}</h4><CodeMirror value={contextContent(item)} indentWithTab={false} extensions={[EditorView.lineWrapping, EditorView.contentAttributes.of({ 'aria-label': `${PROMPT_CONTEXT_LABELS[item.namespace] ?? item.namespace}: ${item.key}`, tabindex: '0' })]} editable={editable && Boolean(onContextChange)} readOnly={!editable || !onContextChange} aria-label={`${PROMPT_CONTEXT_LABELS[item.namespace] ?? item.namespace}: ${item.key}`} onChange={value => editContext(index, value)} /></section> : null)}
      </div> : activeDisabled ? (
        <div>
          <StateCard kind="empty" compact title="No application prompt for this run." action={null} />
        </div>
      ) : (
        <>
          {selectedEntry && <p>{String(selectedEntry.key)} · v{String(selectedEntry.version)}{selectedEntry.overridden === true ? ' · setup override' : ''}</p>}
          {systemLocked && active === 'system' && <p>The shared system prompt is locked for this test.</p>}
          {active === 'application' && (
            <p className="prompt-review-tab-note" data-testid="prompt-tab-note-application">
              Application-wide — saved edits apply to every phase in this run.
              {' '}Use UNKNOWN for missing requirements; include units, evidence sources and observation windows for thresholds.
            </p>
          )}
          <div
            className={`code-viewer prompt-review-editor${editable ? ' editable' : ''}`}
            data-testid={`prompt-tab-editor-${active}`}
          >
            <CodeMirror
              value={activeValue}
              indentWithTab={false}
              extensions={[EditorView.contentAttributes.of({ 'aria-label': activeLabel, tabindex: '0' })]}
              editable={canEdit}
              readOnly={!canEdit}
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
      </TabPanels>
    </div>
  )
}
