/**
 * Time-window model shared by the WindowPresets control and the screens that
 * serialize windows to the URL (D6: /analytics, /logs). Kept apart from the
 * component so the file with JSX only exports components (react-refresh).
 */

export interface TimeWindow {
  /** ISO-8601 window start (omitted = server default). */
  from?: string
  /** ISO-8601 window end (omitted = server default "now"). */
  to?: string
}

export interface WindowPreset {
  label: string
  ms: number
}

export const HOUR_MS = 3_600_000
export const DAY_MS = 24 * HOUR_MS

/** Default presets per the analytics spec: segmented [24h | 7d | 30d]. */
export const DEFAULT_WINDOW_PRESETS: WindowPreset[] = [
  { label: '24h', ms: DAY_MS },
  { label: '7d', ms: 7 * DAY_MS },
  { label: '30d', ms: 30 * DAY_MS },
]

/** Builds the absolute window a relative preset stands for. */
export function presetWindow(ms: number, now: number = Date.now()): TimeWindow {
  return { from: new Date(now - ms).toISOString(), to: new Date(now).toISOString() }
}

/**
 * Which preset (if any) the current window matches: span within a minute of
 * the preset's, so a deep-linked preset URL still highlights after reload.
 */
export function activePresetLabel(
  window: TimeWindow,
  presets: readonly WindowPreset[],
  toleranceMs = 60_000,
): string | null {
  if (!window.from || !window.to) return null
  const from = Date.parse(window.from)
  const to = Date.parse(window.to)
  if (Number.isNaN(from) || Number.isNaN(to)) return null
  const span = to - from
  return presets.find((preset) => Math.abs(span - preset.ms) <= toleranceMs)?.label ?? null
}

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

/** ISO -> datetime-local input value (local time, minute precision). */
export function toLocalInput(iso: string | undefined): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** datetime-local input value -> ISO (undefined when cleared/invalid). */
export function fromLocalInput(value: string): string | undefined {
  if (!value) return undefined
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString()
}
