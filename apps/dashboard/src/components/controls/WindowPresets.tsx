import { useState } from 'react'

import {
  activePresetLabel,
  DEFAULT_WINDOW_PRESETS,
  fromLocalInput,
  presetWindow,
  toLocalInput,
  type TimeWindow,
  type WindowPreset,
} from './timeWindow'
import './controls.css'

/**
 * Shared time-window control (D6: /analytics header + /logs toolbar).
 * Segmented relative presets write absolute ISO from/to (URL-serializable,
 * shareable); a "Custom" toggle collapses two datetime-local inputs for
 * arbitrary windows. The component is controlled — it owns no window state
 * beyond the custom-inputs disclosure. Window math lives in ./timeWindow.
 */
export function WindowPresets({
  value,
  onChange,
  presets = DEFAULT_WINDOW_PRESETS,
  label = 'Time window',
}: {
  value: TimeWindow
  onChange: (next: TimeWindow) => void
  presets?: readonly WindowPreset[]
  label?: string
}) {
  const [customOpen, setCustomOpen] = useState(false)
  const active = activePresetLabel(value, presets)

  return (
    <div className="window-presets" role="group" aria-label={label}>
      <div className="segmented">
        {presets.map((preset) => (
          <button
            key={preset.label}
            type="button"
            className="segmented-btn"
            aria-pressed={active === preset.label}
            onClick={() => onChange(presetWindow(preset.ms))}
          >
            {preset.label}
          </button>
        ))}
        <button
          type="button"
          className="segmented-btn"
          aria-pressed={customOpen}
          aria-expanded={customOpen}
          onClick={() => setCustomOpen((open) => !open)}
        >
          Custom
        </button>
      </div>
      {customOpen && (
        <div className="window-custom">
          <label className="window-custom-field">
            <span>From</span>
            <input
              type="datetime-local"
              className="field-input"
              aria-label={`${label} from`}
              value={toLocalInput(value.from)}
              onChange={(event) =>
                onChange({ ...value, from: fromLocalInput(event.target.value) })
              }
            />
          </label>
          <label className="window-custom-field">
            <span>To</span>
            <input
              type="datetime-local"
              className="field-input"
              aria-label={`${label} to`}
              value={toLocalInput(value.to)}
              onChange={(event) => onChange({ ...value, to: fromLocalInput(event.target.value) })}
            />
          </label>
        </div>
      )}
    </div>
  )
}
