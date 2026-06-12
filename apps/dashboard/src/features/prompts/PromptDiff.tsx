/**
 * Unified diff for prompt versions — @codemirror/merge's unifiedMergeView
 * extension on the existing read-only CodeMirror surface (installed cleanly
 * against the workspace's @codemirror/view 6.43, so no hand-rolled fallback
 * needed). `original` is the comparison base; the rendered document is
 * `value`, with deleted chunks inlined and inserted lines toned via
 * prompts.css on the token sheet.
 */
import { useMemo } from 'react'

import { unifiedMergeView } from '@codemirror/merge'
import CodeMirror from '@uiw/react-codemirror'

export function PromptDiff({
  original,
  value,
  ariaLabel,
}: {
  /** Comparison base (lines only here render as deleted). */
  original: string
  /** The version being viewed (lines only here render as inserted). */
  value: string
  ariaLabel: string
}) {
  const extensions = useMemo(
    () => [unifiedMergeView({ original, mergeControls: false, gutter: true })],
    [original],
  )
  return (
    <div className="code-viewer prompt-diff" role="figure" aria-label={ariaLabel}>
      <CodeMirror
        value={value}
        readOnly
        editable={false}
        basicSetup={{
          lineNumbers: true,
          foldGutter: false,
          highlightActiveLine: false,
          highlightActiveLineGutter: false,
        }}
        extensions={extensions}
      />
    </div>
  )
}
