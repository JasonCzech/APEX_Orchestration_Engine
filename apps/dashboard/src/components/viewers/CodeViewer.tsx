import CodeMirror from '@uiw/react-codemirror'

import { json } from '@codemirror/lang-json'

import './viewers.css'

const JSON_EXTENSIONS = [json()]

/**
 * Read-only CodeMirror surface (mirrors APEX Load's CodeMirror choice).
 * Theming is pure CSS (viewers.css restyles .cm-editor with the token sheet)
 * so the component stays dependency-light — no @codemirror/view import.
 */
export function CodeViewer({
  value,
  language = 'text',
  ariaLabel,
}: {
  value: string
  /** 'json' enables JSON syntax highlighting + fold gutters. */
  language?: 'json' | 'text'
  ariaLabel?: string
}) {
  return (
    <div className="code-viewer" role="figure" aria-label={ariaLabel}>
      <CodeMirror
        value={value}
        readOnly
        editable={false}
        basicSetup={{
          lineNumbers: true,
          foldGutter: language === 'json',
          highlightActiveLine: false,
          highlightActiveLineGutter: false,
        }}
        extensions={language === 'json' ? JSON_EXTENSIONS : []}
      />
    </div>
  )
}
