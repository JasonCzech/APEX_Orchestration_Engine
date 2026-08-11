/**
 * Provider picker + guided option fields, shared by the create panel and the
 * connection detail config tab.
 *
 * The guided form is sugar over the same options JSON the API has always
 * taken: `Custom…` restores the free-text provider and raw textarea, and any
 * option key the catalog does not know is preserved rather than dropped.
 */
import type { PortKind } from '@/api/hooks/useConnections'

import { parseJsonObject } from './adminLogic'
import {
  CUSTOM_PROVIDER,
  buildOptions,
  controlValue,
  providerSpec,
  providersForKind,
  splitOptions,
  visibleOptionFields,
  type OptionsDraft,
  type ProviderSpec,
} from './providerCatalog'

export interface ProviderConnectionFieldsProps {
  kind: PortKind
  provider: string
  /** True when the provider select is on `Custom…`. */
  customProvider: boolean
  onProviderChange: (provider: string, custom: boolean) => void
  draft: OptionsDraft
  onDraftChange: (draft: OptionsDraft) => void
  /** Provider cannot be edited once a connection exists in some surfaces. */
  providerLocked?: boolean
}

/** Serialize guided state into the raw textarea when switching to JSON. */
function toRaw(spec: ProviderSpec | null, draft: OptionsDraft): OptionsDraft {
  // Already-raw text is the user's live edit — never regenerate over it.
  if (draft.mode === 'raw') return draft
  const options = spec === null ? draft.extras : buildOptions(spec, draft)
  return { ...draft, mode: 'raw', rawText: JSON.stringify(options, null, 2) }
}

/** Re-split the raw textarea back into guided fields; blocked while invalid. */
function toGuided(spec: ProviderSpec, draft: OptionsDraft): OptionsDraft {
  const parsed = parseJsonObject(draft.rawText)
  if (!parsed.ok) return draft
  return { ...draft, ...splitOptions(spec, parsed.value), mode: 'guided' }
}

export function ProviderConnectionFields({
  kind,
  provider,
  customProvider,
  onProviderChange,
  draft,
  onDraftChange,
  providerLocked = false,
}: ProviderConnectionFieldsProps) {
  const catalog = providersForKind(kind)
  const spec = customProvider ? null : providerSpec(kind, provider)
  const guidedAvailable = spec !== null
  const showGuided = guidedAvailable && draft.mode === 'guided'
  const rawParse = parseJsonObject(draft.rawText)
  const extraCount = Object.keys(draft.extras).length

  return (
    <>
      {catalog.length > 0 && !providerLocked && (
        <label className="adm-field">
          <span className="adm-field-label">Provider</span>
          <select
            className="field-select"
            aria-label="Provider"
            value={customProvider ? CUSTOM_PROVIDER : provider}
            onChange={(event) => {
              const value = event.target.value
              if (value === CUSTOM_PROVIDER) {
                onProviderChange('', true)
                onDraftChange(toRaw(spec, draft))
                return
              }
              onProviderChange(value, false)
              const next = providerSpec(kind, value)
              if (next === null) {
                // The empty placeholder: serialize current guided values so a
                // detour through it and back does not lose what was typed.
                onDraftChange(toRaw(spec, draft))
                return
              }
              // Re-map the current values against the new spec instead of
              // wiping them: shared fields (base_url, user_email) survive a
              // provider switch and unknown keys stay preserved as extras.
              const parsed = parseJsonObject(draft.rawText)
              if (spec === null || draft.mode === 'raw') {
                if (!parsed.ok) {
                  // Unparsable live JSON: switch the provider but keep the
                  // text and stay raw — regenerating would destroy the edit.
                  onDraftChange({ ...draft, mode: 'raw' })
                  return
                }
                onDraftChange({ ...draft, ...splitOptions(next, parsed.value), mode: 'guided' })
                return
              }
              onDraftChange({
                ...draft,
                ...splitOptions(next, buildOptions(spec, draft)),
                mode: 'guided',
              })
            }}
          >
            <option value="">Select a provider…</option>
            {catalog.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.label}
              </option>
            ))}
            <option value={CUSTOM_PROVIDER}>Custom…</option>
          </select>
        </label>
      )}

      {(catalog.length === 0 || customProvider) && !providerLocked && (
        <label className="adm-field">
          <span className="adm-field-label">Provider name</span>
          <input
            type="text"
            className="field-input"
            aria-label="Provider name"
            placeholder="jira"
            value={provider}
            onChange={(event) => onProviderChange(event.target.value, customProvider)}
          />
          <span className="adm-field-help">must be a registered provider</span>
        </label>
      )}

      {showGuided &&
        spec.controls?.map((control) => (
          <label className="adm-field" key={control.key}>
            <span className="adm-field-label">{control.label}</span>
            <select
              className="field-select"
              aria-label={control.label}
              value={controlValue(spec, draft.controls, control.key)}
              onChange={(event) =>
                onDraftChange({
                  ...draft,
                  controls: { ...draft.controls, [control.key]: event.target.value },
                })
              }
            >
              {control.choices.map((choice) => (
                <option key={choice.value} value={choice.value}>
                  {choice.label}
                </option>
              ))}
            </select>
            {control.help && <span className="adm-field-help">{control.help}</span>}
          </label>
        ))}

      {showGuided &&
        visibleOptionFields(spec, draft.controls).map((field) => (
          <label className="adm-field" key={field.key}>
            <span className="adm-field-label">
              {field.label}
              {field.required ? '' : ' (optional)'}
            </span>
            <input
              type="text"
              className="field-input"
              aria-label={field.label}
              placeholder={field.placeholder}
              value={draft.guided[field.key] ?? ''}
              onChange={(event) =>
                onDraftChange({
                  ...draft,
                  guided: { ...draft.guided, [field.key]: event.target.value },
                })
              }
            />
            {field.help && <span className="adm-field-help">{field.help}</span>}
          </label>
        ))}

      {showGuided && extraCount > 0 && (
        <p className="adm-field-help">
          {extraCount} additional option {extraCount === 1 ? 'key is' : 'keys are'} preserved.
        </p>
      )}

      {!showGuided && (
        <label className="adm-field">
          <span className="adm-field-label">Options (JSON)</span>
          <textarea
            className="field-input adm-json-input"
            aria-label="Options JSON"
            rows={4}
            spellCheck={false}
            value={draft.rawText}
            onChange={(event) => onDraftChange({ ...draft, rawText: event.target.value })}
          />
        </label>
      )}

      {guidedAvailable && (
        <div className="adm-panel-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={!showGuided && !rawParse.ok}
            onClick={() =>
              onDraftChange(showGuided ? toRaw(spec, draft) : toGuided(spec, draft))
            }
          >
            {showGuided ? 'Edit as JSON' : 'Guided fields'}
          </button>
        </div>
      )}
    </>
  )
}
