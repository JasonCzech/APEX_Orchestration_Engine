import '@/components/viewers/viewers.css'
/**
 * Editable CodeMirror surface for prompt content (create panel, new-version
 * mode, playground ad-hoc editor). Same CSS-token skin as CodeViewer
 * (.code-viewer in viewers.css) with an `.editable` caret override.
 */
import { EditorView } from '@codemirror/view'
import CodeMirror from '@uiw/react-codemirror'

export function PromptEditor({
  value,
  onChange,
  ariaLabel,
  disabled = false,
}: {
  value: string
  onChange: (value: string) => void
  ariaLabel: string
  disabled?: boolean
}) {
  return (
    <div className="code-viewer prompt-editor">
      <CodeMirror
        value={value}
        indentWithTab={false}
        extensions={[EditorView.contentAttributes.of({ 'aria-label': ariaLabel, tabindex: '0' })]}
        onChange={onChange}
        aria-label={ariaLabel}
        editable={!disabled}
        readOnly={disabled}
        basicSetup={{
          lineNumbers: true,
          foldGutter: false,
          highlightActiveLine: true,
          highlightActiveLineGutter: true,
        }}
      />
    </div>
  )
}
