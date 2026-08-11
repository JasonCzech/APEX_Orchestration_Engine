/**
 * Per-provider connection option shapes, for guided forms.
 *
 * This is a MIRROR of what each adapter reads out of `ConnectionConfig.options`
 * (see src/apex/adapters/<provider>/), the same way connectionPolicy.ts mirrors
 * the server's scoped-connection policies. There is no provider-discovery
 * endpoint, so a provider the catalog does not know still has to be reachable:
 * every surface keeps a raw-JSON path, and an unregistered provider still gets
 * the server's verbatim 422 listing the ones it does accept.
 *
 * Note base_url: adapters read it from `options` (the server backfills
 * options.base_url from the top-level column at adapter-build time, but only
 * when options lacks it). A guided field marked `mirrorsBaseUrl` submits both
 * so the column display and the effective adapter value can never diverge.
 */
import type { PortKind } from '@/api/hooks/useConnections'

export interface ProviderOptionField {
  /** Key inside the options object, e.g. 'user_email'. */
  key: string
  label: string
  required: boolean
  placeholder?: string
  help?: string
  input: 'text' | 'select'
  choices?: readonly { value: string; label: string }[]
  /** Also submitted as the connection's top-level base_url. */
  mirrorsBaseUrl?: boolean
  /** Hidden unless the named control has this value (pure UI sugar). */
  visibleWhen?: { control: string; equals: string }
}

export interface ProviderControl {
  /** Not an options key — a local toggle that reveals or hides fields. */
  key: string
  label: string
  choices: readonly { value: string; label: string }[]
  help?: string
  /** Which option key's presence means this control is on the second choice. */
  inferFromOption?: string
}

export interface ProviderSpec {
  /** Wire value sent as `provider`. */
  id: string
  label: string
  optionFields: readonly ProviderOptionField[]
  controls?: readonly ProviderControl[]
  secretRef?: { placeholder: string; help: string }
}

export const CUSTOM_PROVIDER = '__custom__'

const JIRA: ProviderSpec = {
  id: 'jira',
  label: 'Jira Cloud',
  optionFields: [
    {
      key: 'base_url',
      label: 'Base URL',
      required: true,
      input: 'text',
      placeholder: 'https://your-site.atlassian.net',
      mirrorsBaseUrl: true,
    },
    {
      key: 'user_email',
      label: 'User email',
      required: true,
      input: 'text',
      placeholder: 'qa@example.com',
      help: 'Atlassian account the API token belongs to',
    },
    {
      key: 'project_key',
      label: 'Project key',
      required: false,
      input: 'text',
      placeholder: 'PHX',
      help: 'Required on a project-scoped connection; scopes every query',
    },
    {
      key: 'sprint_field_id',
      label: 'Sprint field id',
      required: false,
      input: 'text',
      placeholder: 'customfield_10020',
      help: 'Optional — sprint is a per-site custom field',
    },
    {
      key: 'severity_field_id',
      label: 'Severity field id',
      required: false,
      input: 'text',
      placeholder: 'customfield_10050',
      help: 'Optional — severity is a per-site custom field',
    },
  ],
  secretRef: {
    placeholder: 'env:APEX_INTEGRATION_JIRA_API_TOKEN',
    help: 'Atlassian API token, paired with the user email above',
  },
}

const JIRA_ALIGN: ProviderSpec = {
  id: 'jira_align',
  label: 'Jira Align',
  controls: [
    {
      key: 'auth_style',
      label: 'Authentication',
      choices: [
        { value: 'bearer', label: 'Jira Align API token' },
        { value: 'basic', label: 'Atlassian API token (Atlassian Guard)' },
      ],
      help: 'Instances signing in through Atlassian Guard must use the Atlassian token',
      inferFromOption: 'user_email',
    },
  ],
  optionFields: [
    {
      key: 'base_url',
      label: 'Base URL',
      required: true,
      input: 'text',
      placeholder: 'https://your-instance.jiraalign.com',
      mirrorsBaseUrl: true,
    },
    {
      key: 'user_email',
      label: 'User email',
      required: true,
      input: 'text',
      placeholder: 'qa@example.com',
      help: 'Atlassian account the API token belongs to',
      visibleWhen: { control: 'auth_style', equals: 'basic' },
    },
    {
      key: 'program_id',
      label: 'Program id',
      required: false,
      input: 'text',
      placeholder: '42',
      help: 'Numeric. Required on a project-scoped connection; scopes every query',
    },
  ],
  secretRef: {
    placeholder: 'env:APEX_INTEGRATION_JIRA_ALIGN_TOKEN',
    help: 'Read-only access is enough — this connection never writes',
  },
}

const ADO: ProviderSpec = {
  id: 'ado',
  label: 'Azure DevOps',
  optionFields: [
    {
      key: 'base_url',
      label: 'Organization URL',
      required: true,
      input: 'text',
      placeholder: 'https://dev.azure.com/your-org',
      mirrorsBaseUrl: true,
    },
    {
      key: 'project',
      label: 'Project',
      required: false,
      input: 'text',
      placeholder: 'Phoenix',
      help: 'Required on a project-scoped connection; scopes every query',
    },
  ],
  secretRef: {
    placeholder: 'env:APEX_INTEGRATION_ADO_PAT',
    help: 'Personal access token with work-item read access',
  },
}

const STUB: ProviderSpec = {
  id: 'stub',
  label: 'Built-in stub',
  optionFields: [],
}

export const PROVIDER_CATALOG: Partial<Record<PortKind, readonly ProviderSpec[]>> = {
  work_tracking: [JIRA, JIRA_ALIGN, ADO, STUB],
}

export function providersForKind(kind: PortKind): readonly ProviderSpec[] {
  return PROVIDER_CATALOG[kind] ?? []
}

export function providerSpec(kind: PortKind, provider: string): ProviderSpec | null {
  const wanted = provider.trim().toLowerCase()
  return providersForKind(kind).find((spec) => spec.id === wanted) ?? null
}

// ── guided ⇄ raw options ─────────────────────────────────────────────────────

export type GuidedValues = Record<string, string>

export interface OptionsDraft {
  mode: 'guided' | 'raw'
  guided: GuidedValues
  /** Option keys the catalog does not know; preserved so nothing is dropped. */
  extras: Record<string, unknown>
  controls: GuidedValues
  rawText: string
}

export function emptyDraft(): OptionsDraft {
  return { mode: 'guided', guided: {}, extras: {}, controls: {}, rawText: '{}' }
}

function visibleFields(spec: ProviderSpec, controls: GuidedValues): ProviderOptionField[] {
  return spec.optionFields.filter(
    (field) =>
      field.visibleWhen === undefined ||
      controlValue(spec, controls, field.visibleWhen.control) === field.visibleWhen.equals,
  )
}

export function controlValue(
  spec: ProviderSpec,
  controls: GuidedValues,
  key: string,
): string {
  const control = spec.controls?.find((entry) => entry.key === key)
  if (control === undefined) return ''
  return controls[key] ?? control.choices[0]?.value ?? ''
}

export function visibleOptionFields(
  spec: ProviderSpec,
  controls: GuidedValues,
): ProviderOptionField[] {
  return visibleFields(spec, controls)
}

/** Merge guided values over preserved extras into the options object to submit. */
export function buildOptions(
  spec: ProviderSpec,
  draft: OptionsDraft,
): Record<string, unknown> {
  const options: Record<string, unknown> = { ...draft.extras }
  for (const field of visibleFields(spec, draft.controls)) {
    const value = (draft.guided[field.key] ?? '').trim()
    if (value !== '') options[field.key] = value
    // Empty guided value + the key living in extras = a preserved non-string
    // stored value; keep it (fail-closed) until the user types a replacement
    // or removes it via the raw-JSON editor.
    else if (!(field.key in draft.extras)) delete options[field.key]
  }
  // A field hidden by a control must not leak its old value into the payload.
  for (const field of spec.optionFields) {
    if (!visibleFields(spec, draft.controls).includes(field)) delete options[field.key]
  }
  return options
}

/** Split a stored options object into guided values plus untouched extras. */
export function splitOptions(
  spec: ProviderSpec,
  options: Record<string, unknown>,
): Pick<OptionsDraft, 'guided' | 'extras' | 'controls'> {
  const known = new Set(spec.optionFields.map((field) => field.key))
  const guided: GuidedValues = {}
  const extras: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(options)) {
    if (!known.has(key)) {
      extras[key] = value
      continue
    }
    // Known keys are strings in every adapter contract. Numbers are a common
    // raw-JSON slip and coerce losslessly; anything else (null, booleans,
    // objects) must NOT be String()-ified — submitting "null" or
    // "[object Object]" back would corrupt the connection. Nulls mean unset;
    // other garbage is preserved verbatim in extras, because an invalid value
    // fails the adapter closed and silently dropping it on the next save
    // would turn that into a working-but-differently-scoped connection.
    if (typeof value === 'string') guided[key] = value
    else if (typeof value === 'number' && Number.isFinite(value)) guided[key] = String(value)
    else if (value !== null && value !== undefined) extras[key] = value
  }
  const controls: GuidedValues = {}
  for (const control of spec.controls ?? []) {
    const second = control.choices[1]?.value
    if (control.inferFromOption !== undefined && second !== undefined) {
      const present = (guided[control.inferFromOption] ?? '').trim() !== ''
      controls[control.key] = present ? second : (control.choices[0]?.value ?? '')
    }
  }
  return { guided, extras, controls }
}

/** First unmet requirement, as a sentence — or null when the form is complete. */
export function guidedIssue(spec: ProviderSpec, draft: OptionsDraft): string | null {
  for (const field of visibleFields(spec, draft.controls)) {
    if (field.required && (draft.guided[field.key] ?? '').trim() === '') {
      return `${spec.label} requires ${field.label.toLowerCase()}.`
    }
  }
  return null
}

/** The value that must also be submitted as the top-level base_url column. */
export function guidedBaseUrl(spec: ProviderSpec, draft: OptionsDraft): string | null {
  const field = visibleFields(spec, draft.controls).find((entry) => entry.mirrorsBaseUrl)
  if (field === undefined) return null
  return (draft.guided[field.key] ?? '').trim() || null
}
