/**
 * Tiny line-level diff (plain LCS) for the detail page's "diff vs active"
 * indicator. The rich rendered diff is @codemirror/merge's unifiedMergeView
 * (PromptDiff.tsx); this util only counts added/removed lines, so a simple
 * O(n*m) table is fine at prompt sizes (guarded by a line cap).
 */

export interface LineDiffStats {
  added: number
  removed: number
  /** True when the inputs were too large for the LCS table (counts are estimates). */
  truncated: boolean
}

const MAX_LINES = 2000

export function lineDiffStats(before: string, after: string): LineDiffStats {
  if (before === after) return { added: 0, removed: 0, truncated: false }
  const a = before.split('\n')
  const b = after.split('\n')

  if (a.length > MAX_LINES || b.length > MAX_LINES) {
    // Degenerate but honest: report net growth without the LCS pass.
    return {
      added: Math.max(0, b.length - a.length),
      removed: Math.max(0, a.length - b.length),
      truncated: true,
    }
  }

  // LCS length table (single-row rolling to keep memory flat).
  let prev = new Array<number>(b.length + 1).fill(0)
  let curr = new Array<number>(b.length + 1).fill(0)
  for (let i = 1; i <= a.length; i++) {
    for (let j = 1; j <= b.length; j++) {
      curr[j] =
        a[i - 1] === b[j - 1]
          ? (prev[j - 1] ?? 0) + 1
          : Math.max(prev[j] ?? 0, curr[j - 1] ?? 0)
    }
    ;[prev, curr] = [curr, prev]
  }
  const common = prev[b.length] ?? 0
  return { added: b.length - common, removed: a.length - common, truncated: false }
}
