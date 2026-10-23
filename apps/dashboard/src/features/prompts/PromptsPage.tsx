import { PHASE_NAMES } from '@apex/pipeline-events'
import type { PhaseName } from '@apex/pipeline-events'
import { useApplicationsIndex, useEnvironmentsIndex } from '@/api/hooks/useEnvironments'
import { PROMPT_LABELS, parsePromptSelections } from './promptCategories'
import { useTimestampPreference } from '@/theme/preferences'
import { StateCard } from '@/components/ux/StateCard'
import { Skeleton } from '@/components/ux/Skeleton'
/**
 * /prompts — catalog browser. Category filter (?ns), debounced server search
 * (?q: names, keys, descriptions and active text), include-archived
 * (?archived=1), legacy templates (?legacy=1) and sort (?sort=updated). The
 * list is grouped in the order agents receive prompts; choosing a row previews
 * it beside the list, or in a sheet when the page is narrow.
 * [New prompt] (global admin) opens a create panel -> POST -> navigate detail.
 */
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type Ref, type RefObject } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'

import {
  useCreatePrompt,
  useMakeDefaultPrompt,
  usePromptList,
  usePromptById,
  usePromptUsage,
  usePromptVersions,
  type PromptSummary,
} from '@/api/hooks/usePrompts'
import { useAssistants } from '@/api/hooks/useAssistants'
import { isApiError } from '@/api/errors'
import { RequireGlobalAdmin, isGlobalAdmin } from '@/auth/RequireRole'
import { useConsumer } from '@/auth/AuthProvider'
import { CodeViewer } from '@/components/viewers/CodeViewer'
import { Dialog } from '@/components/Dialog'
import { ProblemCard } from '@/components/ProblemCard'
import { PHASE_LABELS } from '@/features/runs/runDisplay'
import { formatRelative } from '@/utils/time'

import { PromptEditor } from './PromptEditor'
import { promptPath } from './promptPaths'
import './prompts.css'

const SEARCH_DEBOUNCE_MS = 300
/** Below this page width the preview opens as a sheet instead of a side panel. */
const NARROW_PAGE_PX = 760
const MAX_KEY_CHARS = 255
/** Groups follow the order agents receive prompts. */
const CATEGORY_ORDER = ['system', 'application', 'environment', 'phase'] as const

const CATEGORY_HELP: Record<string, string> = {
  system: 'Shared instructions every agent phase receives first. Tests use the default unless they choose another.',
  application: 'Knowledge about an application under test, such as its architecture, critical flows and known limits. It is added ahead of each agent phase’s context.',
  environment: 'Facts about one environment, such as topology, capacity and data constraints. It is added for the systems deployed there.',
  phase: 'Instructions for one pipeline phase. Each agent phase uses its default unless a test chooses an alternative.',
}

function errorMessage(error: unknown, fallback: string): string {
  if (isApiError(error)) return error.message
  if (error instanceof Error) return error.message
  return fallback
}

function readableName(value: string): string {
  const words = value.replaceAll(/[_-]+/g, ' ').trim()
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : value
}

function namespaceLabel(namespace: string): string {
  return PROMPT_LABELS[namespace] ?? readableName(namespace)
}

function phaseName(phase: string): string {
  return PHASE_LABELS[phase as PhaseName] ?? readableName(phase)
}

/**
 * Titles for seeded phase prompts. Older installs described them as "Built-in
 * system prompt for the X phase"; newer ones as "Phase context for the x phase".
 */
function seededTitle(description: string | null | undefined): string | null {
  const older = description?.match(/^Built-in (system|user) prompt for the ([a-z_]+) phase$/)
  if (older) return `${phaseName(older[2]!)} ${older[1] === 'system' ? 'phase context' : 'request template'}`
  const newer = description?.match(/^(Phase context|Legacy request template) for the ([a-z ]+) phase$/)
  if (newer) return `${phaseName(newer[2]!.replaceAll(' ', '_'))} ${newer[1] === 'Phase context' ? 'phase context' : 'request template'}`
  return null
}

function promptTitle(prompt: PromptSummary): string {
  return seededTitle(prompt.description) ?? (prompt.description?.trim() || readableName(prompt.key))
}

function applicability(prompt: PromptSummary, names: Record<string, string>): string {
  if (!prompt.selection_enabled) return 'Legacy template · not selectable in new tests'
  switch (prompt.namespace) {
    case 'system': return 'All agent phases'
    case 'phase': return prompt.phase ? phaseName(prompt.phase) : 'No phase assigned'
    case 'application': return prompt.is_shared ? 'Shared across applications' : names[prompt.application_id ?? ''] ?? 'Application unavailable'
    case 'environment': return names[prompt.environment_id ?? ''] ?? 'Environment unavailable'
    default: return namespaceLabel(prompt.namespace)
  }
}

interface PromptGroup {
  id: string
  title: string
  note?: string
  rows: PromptSummary[]
}

function byPipeline(a: PromptSummary, b: PromptSummary): number {
  const phase = (row: PromptSummary) => (row.phase ? PHASE_NAMES.indexOf(row.phase) : PHASE_NAMES.length)
  return phase(a) - phase(b) || Number(b.is_default) - Number(a.is_default) || a.key.localeCompare(b.key)
}

const byTitle = (a: PromptSummary, b: PromptSummary) => promptTitle(a).localeCompare(promptTitle(b))
const byUpdated = (a: PromptSummary, b: PromptSummary) => (b.updated_at ?? '').localeCompare(a.updated_at ?? '') || byTitle(a, b)

function promptGroups(rows: PromptSummary[], namespace: string, names: Record<string, string>, showLegacy: boolean, recent: boolean): PromptGroup[] {
  const selectable = rows.filter(row => row.selection_enabled)
  const legacy = showLegacy ? rows.filter(row => !row.selection_enabled) : []
  if (recent) {
    const listed = namespace ? selectable.filter(row => row.namespace === namespace) : [...selectable, ...legacy]
    return listed.length ? [{ id: 'recent', title: '', rows: [...listed].sort(byUpdated) }] : []
  }
  if (!namespace) {
    const known = new Set<string>(CATEGORY_ORDER)
    const groups: PromptGroup[] = CATEGORY_ORDER.map(ns => ({
      id: ns,
      title: namespaceLabel(ns),
      rows: selectable.filter(row => row.namespace === ns).sort(ns === 'phase' ? byPipeline : byTitle),
    }))
    // The API accepts any namespace; keep unfamiliar selectable prompts visible.
    groups.push({ id: 'other', title: 'Other prompts', rows: selectable.filter(row => !known.has(row.namespace)).sort(byTitle) })
    groups.push({ id: 'legacy', title: 'Legacy templates', note: 'Kept for older tests. New tests can’t select these.', rows: legacy.sort(byPipeline) })
    return groups.filter(group => group.rows.length)
  }
  const inCategory = selectable.filter(row => row.namespace === namespace)
  if (namespace === 'system') return inCategory.length ? [{ id: 'system', title: '', rows: inCategory.sort(byTitle) }] : []
  const grouped = new Map<string, PromptSummary[]>()
  for (const row of inCategory) {
    const id = namespace === 'phase' ? row.phase ?? 'unassigned' : namespace === 'application' ? (row.is_shared ? 'shared' : row.application_id ?? 'unassigned') : row.environment_id ?? 'unassigned'
    grouped.set(id, [...(grouped.get(id) ?? []), row])
  }
  const title = (id: string) => id === 'shared' ? 'Shared across applications' : id === 'unassigned' ? 'Unassigned' : namespace === 'phase' ? phaseName(id) : names[id] ?? readableName(id)
  const groups = [...grouped.entries()]
    .sort(([a], [b]) => namespace === 'phase' ? PHASE_NAMES.indexOf(a as PhaseName) - PHASE_NAMES.indexOf(b as PhaseName) : title(a).localeCompare(title(b)))
    .map(([id, groupRows]) => ({ id, title: title(id), rows: groupRows.sort(byPipeline) }))
  // Phase rows name their phase. Headers only help when some phase has alternatives,
  // and then every phase gets one so heading navigation stays accurate.
  if (namespace === 'phase' && groups.every(group => group.rows.length === 1)) return groups.map(group => ({ ...group, title: '' }))
  return groups
}

/** True when the element is narrower than `width`; measured before paint to avoid a layout flash. */
function useNarrow(ref: RefObject<HTMLElement | null>, width: number): boolean {
  const [narrow, setNarrow] = useState(false)
  useLayoutEffect(() => {
    const element = ref.current
    if (!element) return undefined
    const apply = (measured: number) => setNarrow(measured > 0 && measured < width)
    apply(element.getBoundingClientRect().width)
    if (typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(([entry]) => apply(entry?.contentRect.width ?? 0))
    observer.observe(element)
    return () => observer.disconnect()
  }, [ref, width])
  return narrow
}

function PromptItem({ prompt, names, selected, onSelect }: { prompt: PromptSummary; names: Record<string, string>; selected: boolean; onSelect: () => void }) {
  const archived = Boolean(prompt.archived_at)
  const id = useId()
  return (
    <li className={`prompts-row${archived ? ' prompts-row-archived' : ''}${selected ? ' prompts-row-selected' : ''}`} data-testid={`prompt-row-${prompt.id}`}>
      {/* Named by its title; the key and status lines describe it. */}
      <button type="button" className="prompts-item" aria-pressed={selected} onClick={onSelect}
        aria-labelledby={`${id}-title`} aria-describedby={`${id}-chips ${id}-meta`}>
        <span className="prompts-item-line">
          <span id={`${id}-title`} className="prompts-item-title">{promptTitle(prompt)}</span>
          <span id={`${id}-chips`} className="prompts-badges">
            {prompt.active_version && <span className="dash-context-chip prompts-version-chip">v{prompt.active_version.version}</span>}
            {prompt.is_default && <span className="topbar-meta-chip">default</span>}
            {archived && <span className="topbar-meta-chip prompts-archived-chip">archived</span>}
          </span>
        </span>
        <span id={`${id}-meta`} className="prompts-item-meta">
          <code className="prompts-item-key">{prompt.key}</code>
          <span>{applicability(prompt, names)}</span>
          {prompt.updated_at && <span title={prompt.updated_at}>Updated {formatRelative(prompt.updated_at)}</span>}
        </span>
      </button>
    </li>
  )
}

function VersionSummary({ prompt }: { prompt: PromptSummary }) {
  const versions = usePromptVersions(prompt.namespace, prompt.key, prompt.id)
  if (!versions.data?.length) return null
  const latest = versions.data.reduce((max, item) => (item.version > max.version ? item : max))
  const pending = prompt.active_version && latest.version > prompt.active_version.version
  return (
    <>
      <span className="prompts-muted">{versions.data.length} {versions.data.length === 1 ? 'version' : 'versions'}</span>
      {pending && <span className="topbar-meta-chip prompts-pending-chip">v{latest.version} saved, not active</span>}
    </>
  )
}

function slotLabel(slot: string): string {
  return slot.startsWith('phase:') ? `${phaseName(slot.slice('phase:'.length))} phase` : namespaceLabel(slot)
}

/** Test presets that select this prompt, and the tests that pinned it. */
function UsedBy({ prompt }: { prompt: PromptSummary }) {
  const headingId = useId()
  const usage = usePromptUsage(prompt.id)
  const presets = useAssistants()
  const usingPresets = (presets.data ?? []).filter(preset => {
    const selections = parsePromptSelections(preset.configurable['prompt_selections'])
    if (!selections) return false
    return [selections.system, ...Object.values(selections.phases), ...selections.application, ...selections.environment].some(item => item?.prompt_id === prompt.id)
  })
  const tests = usage.data?.tests ?? []
  const total = usage.data?.total_tests ?? 0
  return (
    <section className="prompts-used-by" aria-labelledby={headingId}>
      <h3 id={headingId}>Used by</h3>
      {usingPresets.length > 0 && <ul className="prompts-used-list" aria-label="Test presets">
        {usingPresets.map(preset => <li key={preset.assistantId}><span className="prompts-muted">Preset</span> <Link to={`/test-presets/${encodeURIComponent(preset.assistantId)}`}>{preset.name}</Link></li>)}
      </ul>}
      {usage.isPending ? <p className="prompts-muted">Loading tests…</p>
        : usage.isError ? <p className="prompts-muted">Tests that used this prompt couldn’t be loaded. <button type="button" className="btn btn-ghost btn-sm" onClick={() => void usage.refetch()}>Retry</button></p>
        : tests.length > 0 ? <>
          <ul className="prompts-used-list" aria-label="Tests">
            {tests.map(test => (
              <li key={test.thread_id}>
                <Link to={`/runs/${encodeURIComponent(test.thread_id)}`}>{test.title || 'Untitled test'}</Link>
                <span className="prompts-muted"> · v{test.version} as {slotLabel(test.slot).toLowerCase()}{test.recorded_at ? ` · ${formatRelative(test.recorded_at)}` : ''}</span>
              </li>
            ))}
          </ul>
          {total > tests.length && <p className="prompts-muted">{`and ${total - tests.length} more ${total - tests.length === 1 ? 'test' : 'tests'}`}</p>}
        </>
        : presets.isPending ? null
        : usingPresets.length === 0 && <p className="prompts-muted">{presets.isError ? 'No recorded test has pinned this prompt yet.' : 'No test preset selects this prompt, and no recorded test has pinned it yet.'}</p>}
      {presets.isError && <p className="prompts-muted">Test presets couldn’t be loaded, so presets that use this prompt aren’t listed.</p>}
      <p className="prompts-muted prompts-used-note">Tests are recorded when they launch, from this release on.</p>
    </section>
  )
}

function PromptPreview({ prompt, names, headingRef }: { prompt: PromptSummary | undefined; names: Record<string, string>; headingRef?: Ref<HTMLHeadingElement> }) {
  const makeDefault = useMakeDefaultPrompt()
  const detailQuery = usePromptById(prompt?.namespace ?? '', prompt?.key ?? '', prompt?.id)
  if (!prompt) {
    return <>
      <h2>Choose a prompt</h2>
      <p className="prompts-muted">Choose a prompt to read its active version without leaving the catalog.</p>
    </>
  }
  const title = promptTitle(prompt)
  const header = (
    <div className="prompts-preview-header">
      <p className="prompts-preview-kicker">{namespaceLabel(prompt.namespace)} · {applicability(prompt, names)}</p>
      <h2 ref={headingRef} tabIndex={-1}>{title}</h2>
      <code className="prompts-key-meta">{prompt.key}</code>
      <div className="prompts-preview-meta">
        {prompt.active_version && <span className="dash-context-chip prompts-version-chip">Active v{prompt.active_version.version}</span>}
        {prompt.is_default && <span className="dash-context-chip">Default</span>}
        {prompt.archived_at && <span className="topbar-meta-chip prompts-archived-chip">archived</span>}
        <VersionSummary prompt={prompt} />
        {prompt.updated_at && <span className="prompts-muted" title={prompt.updated_at}>Updated {formatRelative(prompt.updated_at)}</span>}
      </div>
      <div className="prompts-preview-actions">
        <Link className="btn btn-primary btn-sm" to={promptPath(prompt.namespace, prompt.key)}>Open full editor</Link>
        {prompt.selection_enabled && !prompt.archived_at && !prompt.is_default && ['system', 'phase'].includes(prompt.namespace) && <RequireGlobalAdmin><button type="button" className="btn btn-secondary btn-sm" disabled={makeDefault.isPending} onClick={() => makeDefault.mutate(prompt.id)}>Make default</button></RequireGlobalAdmin>}
      </div>
      {makeDefault.isError && <p role="alert">{makeDefault.error.message}</p>}
    </div>
  )
  const detail = detailQuery.data
  const description = detail?.description?.trim()
  // Header, body and "Used by" keep fixed positions so loading never remounts them.
  return <>
    {header}
    {detailQuery.isPending ? <Skeleton label="Loading active prompt" rows={2} />
      : !detail ? <div className="prompts-preview-error">
        <p className="prompts-muted">{errorMessage(detailQuery.error, 'The active prompt could not be loaded.')}</p>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => void detailQuery.refetch()}>Retry</button>
      </div>
      : <div className="prompts-preview-body">
        {description && description !== title && !seededTitle(description) && <p className="prompt-description">{description}</p>}
        {detail.content ? (
          <CodeViewer value={detail.content} ariaLabel={`Active prompt content for ${detail.key}`} />
        ) : (
          <StateCard kind="empty" compact title="No active prompt content." action={null} />
        )}
        {detail.note && <p className="prompts-muted">Version note: {detail.note}</p>}
      </div>}
    {prompt.selection_enabled && <UsedBy prompt={prompt} />}
  </>
}

interface CreateDraft {
  namespace: string
  phase: PhaseName | null
  application_id: string
  environment_id: string
  is_shared: boolean
  is_default: boolean
  key: string
  description: string
  content: string
  note: string
  copy_from: string
}

function emptyDraft(namespace: string): CreateDraft {
  return { namespace, phase: null, application_id: '', environment_id: '', is_shared: false, is_default: false, key: '', description: '', content: '', note: '', copy_from: '' }
}

function keyProblem(draft: CreateDraft, existing: PromptSummary[]): string | null {
  const key = draft.key.trim()
  if (!key) return null
  if (key.length > MAX_KEY_CHARS) return `Use at most ${MAX_KEY_CHARS} characters.`
  if (/\s/.test(key)) return 'Use letters, numbers, hyphens, underscores, dots or slashes, with no spaces.'
  // Keys are unique per category, archived prompts included.
  if (existing.some(row => row.namespace === draft.namespace && row.key === key)) return `A ${namespaceLabel(draft.namespace).toLowerCase()} prompt with this key already exists.`
  return null
}

function CreatePromptPanel({ initialNamespace, initialEnvironmentId = '', onClose }: { initialNamespace: string; initialEnvironmentId?: string; onClose: () => void }) {
  const navigate = useNavigate()
  const create = useCreatePrompt()
  const applications = useApplicationsIndex()
  const environments = useEnvironmentsIndex()
  // The whole catalog, not the filtered page: uniqueness and "Start from" span it.
  const catalog = usePromptList({ includeArchived: true })
  const existing = useMemo(() => catalog.data ?? [], [catalog.data])
  const [draft, setDraft] = useState<CreateDraft>(() => ({ ...emptyDraft(initialNamespace), environment_id: initialNamespace === 'environment' ? initialEnvironmentId : '' }))
  const [pendingSource, setPendingSource] = useState<string | null>(null)
  const keyHelpId = useId()
  const source = existing.find(row => row.id === draft.copy_from)
  const sourceDetail = usePromptById(source?.namespace ?? '', source?.key ?? '', source?.id)
  // What the content was when a copy was requested, the text last copied in,
  // and the latest content (to notice edits made while the source loads).
  const copyBaseline = useRef<string | null>(null)
  const copiedText = useRef<string | null>(null)
  const latestContent = useRef(draft.content)
  const [skippedCopy, setSkippedCopy] = useState<string | null>(null)
  useEffect(() => {
    latestContent.current = draft.content
  }, [draft.content])
  useEffect(() => {
    if (!source || copyBaseline.current === null || sourceDetail.data?.content == null) return
    const text = sourceDetail.data.content
    const edited = latestContent.current !== copyBaseline.current
    copyBaseline.current = null
    if (edited) {
      // Don't overwrite edits made while the source was loading; say so instead.
      setDraft(prev => ({ ...prev, copy_from: '' }))
      setSkippedCopy(promptTitle(source))
      return
    }
    setDraft(prev => ({ ...prev, content: text }))
    copiedText.current = text
  }, [source, sourceDetail.data])

  function chooseSource(id: string) {
    setPendingSource(null)
    setSkippedCopy(null)
    if (!id) {
      // Back to blank: clear the copied text unless it has been edited.
      const copied = copiedText.current
      copiedText.current = null
      setDraft(prev => ({ ...prev, copy_from: '', content: prev.content === copied ? '' : prev.content }))
      return
    }
    copyBaseline.current = draft.content
    setDraft(prev => ({ ...prev, copy_from: id }))
  }
  function requestSource(id: string) {
    const untouched = !draft.content.trim() || draft.content === copiedText.current
    if (id && !untouched) setPendingSource(id)
    else chooseSource(id)
  }

  const problem = keyProblem(draft, existing)
  const valid =
    draft.namespace.trim().length > 0 && draft.key.trim().length > 0 && !problem && draft.content.trim().length > 0 &&
    (draft.namespace !== 'phase' || Boolean(draft.phase)) &&
    (draft.namespace !== 'application' || draft.is_shared || Boolean(draft.application_id)) &&
    (draft.namespace !== 'environment' || Boolean(draft.environment_id))
  const copyOptions = existing.filter(row => row.selection_enabled && !row.archived_at && row.namespace === draft.namespace)
  const pendingTitle = existing.find(row => row.id === pendingSource)

  function submit() {
    if (!valid || create.isPending) return
    create.mutate(
      {
        namespace: draft.namespace.trim(),
        selection_enabled: true,
        is_default: ['system', 'phase'].includes(draft.namespace) && draft.is_default,
        phase: draft.namespace === 'phase' ? draft.phase : null,
        application_id: draft.namespace === 'application' && !draft.is_shared ? draft.application_id : null,
        is_shared: draft.namespace === 'application' && draft.is_shared,
        environment_id: draft.namespace === 'environment' ? draft.environment_id : null,
        key: draft.key.trim(),
        content: draft.content,
        ...(draft.description.trim() ? { description: draft.description.trim() } : {}),
        ...(draft.note.trim() ? { note: draft.note.trim() } : {}),
      },
      {
        onSuccess: (created) => {
          onClose()
          void navigate(promptPath(created.namespace, created.key))
        },
      },
    )
  }

  return (
    <Dialog
      overlayClassName="prompt-modal-overlay"
      className="prompt-modal glass-panel"
      ariaLabel="New prompt"
      onClose={onClose}
      closeOnBackdrop={!create.isPending}
      closeOnEscape={!create.isPending}
    >
      <h2 className="prompt-modal-title">New prompt</h2>
      <p className="prompts-muted">Version 1 is active as soon as the prompt is created, so new tests can select it. Existing tests keep the versions they pinned.</p>
      <div className="prompt-form-grid">
        <label className="prompt-field">
          <span className="prompt-field-label">Category</span>
          <select className="field-input" value={draft.namespace} aria-label="Category" onChange={event => { setPendingSource(null); setDraft({ ...emptyDraft(event.target.value), key: draft.key, description: draft.description, content: draft.content, note: draft.note }) }}>
            {CATEGORY_ORDER.map(ns => <option key={ns} value={ns}>{namespaceLabel(ns)}</option>)}
          </select>
        </label>
        {draft.namespace === 'phase' && <label className="prompt-field">
          <span className="prompt-field-label">Phase</span>
          <select className="field-input" aria-label="Phase" value={draft.phase ?? ''} onChange={event => setDraft({ ...draft, phase: (event.target.value || null) as PhaseName | null })}>
            <option value="">Choose a phase</option>
            {PHASE_NAMES.map(phase => <option key={phase} value={phase}>{phaseName(phase)}</option>)}
          </select>
        </label>}
        {draft.namespace === 'application' && <label className="prompt-field">
          <span className="prompt-field-label">Applies to</span>
          <select className="field-input" aria-label="Application applicability" value={draft.is_shared ? 'shared' : 'application'} onChange={event => setDraft({ ...draft, is_shared: event.target.value === 'shared' })}>
            <option value="application">One application</option>
            <option value="shared">All applications (shared)</option>
          </select>
        </label>}
        {draft.namespace === 'application' && !draft.is_shared && <label className="prompt-field">
          <span className="prompt-field-label">Application</span>
          <select className="field-input" aria-label="Application" value={draft.application_id} onChange={event => setDraft({ ...draft, application_id: event.target.value })}>
            <option value="">Choose an application</option>
            {applications.data?.map(app => <option key={app.id} value={app.id}>{app.name}</option>)}
          </select>
        </label>}
        {draft.namespace === 'environment' && <label className="prompt-field">
          <span className="prompt-field-label">Environment</span>
          <select className="field-input" aria-label="Environment" value={draft.environment_id} onChange={event => setDraft({ ...draft, environment_id: event.target.value })}>
            <option value="">Choose an environment</option>
            {environments.data?.map(env => <option key={env.id} value={env.id}>{applications.data?.find(app => app.id === env.application_id)?.name ?? env.application_id} / {env.name}</option>)}
          </select>
        </label>}
      </div>
      <p className="prompts-muted prompt-field-help">{CATEGORY_HELP[draft.namespace]}</p>
      <div className="prompt-form-grid">
        <label className="prompt-field">
          <span className="prompt-field-label">Key</span>
          <input className="field-input" value={draft.key} onChange={event => setDraft({ ...draft, key: event.target.value })} placeholder="checkout-architecture"
            aria-label="Key" aria-describedby={keyHelpId} aria-invalid={problem ? true : undefined} />
          <span id={keyHelpId} aria-live="polite" className={problem ? 'prompt-field-help prompt-field-help--error' : 'prompts-muted prompt-field-help'}>
            {problem ?? 'Unique within the category. Agents see it in the context header, so keep it short and descriptive.'}
          </span>
        </label>
        <label className="prompt-field">
          <span className="prompt-field-label">Description</span>
          <input className="field-input" value={draft.description} onChange={event => setDraft({ ...draft, description: event.target.value })} aria-label="Description" placeholder="Shown as the prompt’s title in the catalog" />
        </label>
      </div>
      {['system', 'phase'].includes(draft.namespace) && <label className="prompt-field prompt-field--check">
        <input type="checkbox" checked={draft.is_default} onChange={event => setDraft({ ...draft, is_default: event.target.checked })} />
        <span>Make this the default {draft.namespace === 'phase' ? 'for its phase' : 'system prompt'}<span className="prompts-muted"> · used when a test doesn’t choose another</span></span>
      </label>}
      <div className="prompt-field">
        <div className="prompt-field-row">
          <span className="prompt-field-label">Content</span>
          {copyOptions.length > 0 && <label className="prompt-copy-from">
            <span className="prompts-muted">Start from</span>
            <select className="field-input" aria-label="Start from an existing prompt" value={draft.copy_from} onChange={event => requestSource(event.target.value)}>
              <option value="">Blank</option>
              {copyOptions.map(row => <option key={row.id} value={row.id}>{promptTitle(row)}</option>)}
            </select>
          </label>}
        </div>
        {pendingTitle && <div className="tonal-card warning prompt-copy-confirm" role="alert">
          <span>Replace what you’ve written with “{promptTitle(pendingTitle)}”?</span>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => chooseSource(pendingTitle.id)}>Replace content</button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setPendingSource(null)}>Keep mine</button>
        </div>}
        {skippedCopy && <p className="prompts-muted" role="status">You edited the content while “{skippedCopy}” was loading, so it wasn’t copied in.</p>}
        <PromptEditor value={draft.content} onChange={(content) => setDraft((prev) => ({ ...prev, content }))} ariaLabel="Prompt content" />
      </div>
      <label className="prompt-field">
        <span className="prompt-field-label">Version note</span>
        <input className="field-input" value={draft.note} onChange={event => setDraft({ ...draft, note: event.target.value })} placeholder="Why this version exists" aria-label="Version note" />
      </label>
      {create.isError && <div className="tonal-card danger" role="alert">{errorMessage(create.error, 'Create failed.')}</div>}
      <div className="prompt-modal-actions">
        <button type="button" className="btn btn-ghost" onClick={onClose} disabled={create.isPending}>Cancel</button>
        <button type="button" className="btn btn-primary" onClick={submit} disabled={!valid || create.isPending}>
          {create.isPending ? 'Creating…' : 'Create prompt (v1 active)'}
        </button>
      </div>
    </Dialog>
  )
}

export function PromptsPage() {
  useTimestampPreference()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const selectedNs = searchParams.get('ns') ?? ''
  const selectedId = searchParams.get('prompt') ?? ''
  const includeArchived = searchParams.get('archived') === '1'
  const showLegacy = searchParams.get('legacy') === '1'
  const recent = searchParams.get('sort') === 'updated'
  const committedQ = searchParams.get('q') ?? ''
  const [search, setSearch] = useState(committedQ)
  const [creating, setCreating] = useState<string | null>(null)
  const [creatingEnvironmentId, setCreatingEnvironmentId] = useState('')
  const consumer = useConsumer()
  // Other pages link here to start a prompt, e.g. ?new=environment&environment_id=…
  const newCategory = searchParams.get('new')
  useEffect(() => {
    if (!newCategory) return
    if (isGlobalAdmin(consumer) && (CATEGORY_ORDER as readonly string[]).includes(newCategory)) {
      setCreating(newCategory)
      setCreatingEnvironmentId(searchParams.get('environment_id') ?? '')
    }
    setSearchParams(prev => { const next = new URLSearchParams(prev); next.delete('new'); next.delete('environment_id'); return next }, { replace: true })
  }, [newCategory, consumer, searchParams, setSearchParams])
  const pageRef = useRef<HTMLElement>(null)
  const narrow = useNarrow(pageRef, NARROW_PAGE_PX)
  const sheetHeadingRef = useRef<HTMLHeadingElement>(null)
  // The sheet opens only when a row is chosen on a narrow page, never on resize.
  const [sheetRequested, setSheetRequested] = useState(false)
  const sheetPushed = useRef(false)

  useEffect(() => {
    setSearch(committedQ)
  }, [committedQ])
  useEffect(() => {
    const trimmed = search.trim()
    if (trimmed === committedQ) return undefined
    const id = window.setTimeout(() => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev)
        if (trimmed) next.set('q', trimmed)
        else next.delete('q')
        next.delete('prompt')
        return next
      })
    }, SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(id)
  }, [search, committedQ, setSearchParams])

  const { data, error, isPending, isError, isPlaceholderData, refetch } = usePromptList({
    includeArchived,
    q: committedQ || undefined,
  })
  const applications = useApplicationsIndex()
  const environments = useEnvironmentsIndex()
  const names = useMemo(() => {
    const result: Record<string, string> = {}
    for (const app of applications.data ?? []) result[app.id] = app.name
    for (const env of environments.data ?? []) result[env.id] = `${result[env.application_id] ?? env.application_id} / ${env.name}`
    return result
  }, [applications.data, environments.data])

  const all = useMemo(() => data ?? [], [data])
  const counts = useMemo(() => Object.fromEntries(CATEGORY_ORDER.map(ns => [ns, all.filter(row => row.namespace === ns && row.selection_enabled).length])), [all])
  const otherCount = all.filter(row => row.selection_enabled && !(CATEGORY_ORDER as readonly string[]).includes(row.namespace)).length
  const legacyCount = all.filter(row => !row.selection_enabled).length
  // A search looks everywhere, so it includes legacy templates.
  const legacyIncluded = showLegacy || Boolean(committedQ)
  const groups = useMemo(() => promptGroups(all, selectedNs, names, legacyIncluded && !selectedNs, recent), [all, selectedNs, names, legacyIncluded, recent])
  // Selection follows what is on screen: the first visible row unless one is chosen.
  const visible = groups.flatMap(group => group.rows)
  const chosen = visible.find(row => row.id === selectedId)
  const selectedPrompt = chosen ?? visible[0]
  const total = CATEGORY_ORDER.reduce((sum, ns) => sum + counts[ns]!, 0) + otherCount + (legacyIncluded ? legacyCount : 0)
  const loaded = !isPending && !isPlaceholderData
  const sheetOpen = narrow && sheetRequested && Boolean(chosen)

  useEffect(() => {
    if (sheetOpen) sheetHeadingRef.current?.focus()
  }, [sheetOpen, chosen?.id])
  useEffect(() => {
    setSheetRequested(false)
  }, [narrow])
  // A link to a prompt that isn't listed here (filtered out or legacy) shouldn't linger.
  useEffect(() => {
    if (loaded && selectedId && !chosen) {
      setSearchParams(prev => { const next = new URLSearchParams(prev); next.delete('prompt'); return next }, { replace: true })
    }
  }, [loaded, selectedId, chosen, setSearchParams])

  function update(change: (next: URLSearchParams) => void, replace = false) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      change(next)
      return next
    }, { replace })
  }
  const selectNamespace = (ns: string) => update(next => { if (ns) next.set('ns', ns); else next.delete('ns'); next.delete('prompt') })
  const toggleArchived = (checked: boolean) => update(next => { if (checked) next.set('archived', '1'); else next.delete('archived'); next.delete('prompt') })
  const toggleLegacy = (checked: boolean) => update(next => { if (checked) next.set('legacy', '1'); else next.delete('legacy'); next.delete('prompt') })
  const setSort = (value: string) => update(next => { if (value === 'updated') next.set('sort', 'updated'); else next.delete('sort'); next.delete('prompt') }, true)
  function selectPrompt(promptId: string) {
    if (narrow) {
      // The sheet is a view of its own, so Back closes it.
      sheetPushed.current = true
      setSheetRequested(true)
      update(next => next.set('prompt', promptId))
      return
    }
    // Previewing beside the list is browsing, not navigation: no history entry.
    update(next => next.set('prompt', promptId), true)
  }
  function closeSheet() {
    const id = chosen?.id
    setSheetRequested(false)
    if (sheetPushed.current) {
      sheetPushed.current = false
      void navigate(-1)
    } else {
      update(next => next.delete('prompt'), true)
    }
    // Return focus to the row the sheet was opened from.
    window.requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-testid="prompt-row-${id}"] .prompts-item`)?.focus())
  }

  const filters = [
    { ns: '', label: 'All prompts', count: total },
    ...CATEGORY_ORDER.map(ns => ({ ns, label: namespaceLabel(ns), count: counts[ns]! })),
  ]

  function emptyState() {
    if (committedQ) {
      return <StateCard kind="filtered" title="No prompts found" action={<button className="btn btn-secondary" onClick={() => { setSearch(''); update(next => { next.delete('q'); next.delete('prompt') }) }}>Clear search</button>}>
        No prompts match “{committedQ}”. Search covers names, keys, descriptions and the text of each active version.
      </StateCard>
    }
    if (selectedNs) {
      return <StateCard kind="empty" title={`No ${namespaceLabel(selectedNs).toLowerCase()} yet`} action={<RequireGlobalAdmin><button className="btn btn-primary" onClick={() => setCreating(selectedNs)}>New {namespaceLabel(selectedNs).toLowerCase()}</button></RequireGlobalAdmin>}>
        {CATEGORY_HELP[selectedNs]}
      </StateCard>
    }
    return <StateCard kind="empty" title="No prompts yet" action={<RequireGlobalAdmin><button className="btn btn-primary" onClick={() => setCreating('system')}>Create prompt</button></RequireGlobalAdmin>}>
      Create the first prompt to seed the catalog.
    </StateCard>
  }

  return (
    <section className="prompts-page animate-enter" ref={pageRef}>
      <header className="prompts-toolbar glass-panel">
        <input
          type="search"
          className="field-input prompts-search"
          placeholder="Search names, keys and prompt text…"
          aria-label="Search prompts"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <label className="prompts-sort">
          <span className="prompts-muted">Sort</span>
          <select className="field-input" aria-label="Sort prompts" value={recent ? 'updated' : 'agent'} onChange={event => setSort(event.target.value)}>
            <option value="agent">Agent order</option>
            <option value="updated">Recently updated</option>
          </select>
        </label>
        <label className="prompts-archived-toggle">
          <input type="checkbox" checked={includeArchived} onChange={(event) => toggleArchived(event.target.checked)} />
          Include archived
        </label>
        {legacyCount > 0 && !selectedNs && !committedQ && <label className="prompts-archived-toggle">
          <input type="checkbox" checked={showLegacy} onChange={(event) => toggleLegacy(event.target.checked)} />
          Show legacy templates ({legacyCount})
        </label>}
        <RequireGlobalAdmin>
          <button type="button" className="btn btn-primary prompts-new-btn" onClick={() => setCreating(selectedNs || 'application')}>New prompt</button>
        </RequireGlobalAdmin>
      </header>
      <p className="prompts-intro">
        Tests choose their prompts on <Link to="/runs/new?step=prompts">New test → Prompts</Link>. Agents receive the system prompt, then application and environment context, then the phase context. The chosen versions are pinned when a test launches.
      </p>

      <nav className="prompts-tree glass-panel" aria-label="Prompt categories">
        {filters.map(({ ns, label, count }) => (
          <button key={ns || 'all'} type="button" className={`prompts-tree-item${selectedNs === ns ? ' active' : ''}`}
            aria-pressed={selectedNs === ns} aria-label={`${label}, ${count}`} onClick={() => selectNamespace(ns)}>
            <span>{label}</span>
            <span className="prompts-tree-count" aria-hidden="true">{count}</span>
          </button>
        ))}
      </nav>
      {selectedNs && CATEGORY_HELP[selectedNs] && visible.length > 0 && <p className="prompts-muted prompts-category-help">{CATEGORY_HELP[selectedNs]}</p>}

      <div className="prompts-main">
        {isError && (!data || isPlaceholderData) ? (
          <ProblemCard title="Prompt catalog unavailable" message={errorMessage(error, 'The prompt list could not be loaded.')} onRetry={() => refetch()} />
        ) : isPending || isPlaceholderData ? (
          <Skeleton label="Loading prompts" rows={5} />
        ) : visible.length === 0 ? (
          emptyState()
        ) : (
          <>
            {isError && (
              <div className="runs-refresh-error" role="alert">
                <span>Refresh failed: {errorMessage(error, 'The prompt list could not be loaded.')}</span>
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => refetch()}>Retry</button>
              </div>
            )}
            <div className={`prompts-browser${narrow ? ' prompts-browser--narrow' : ''}`}>
              <div className="prompts-list glass-panel">
                {groups.map(group => <PromptGroupSection key={group.id} group={group}>
                  {group.rows.map(prompt => (
                    <PromptItem key={prompt.id} prompt={prompt} names={names} selected={selectedPrompt?.id === prompt.id && (!narrow || Boolean(chosen))} onSelect={() => selectPrompt(prompt.id)} />
                  ))}
                </PromptGroupSection>)}
              </div>
              {!narrow && (
                <aside className="prompts-preview glass-panel" aria-label="Prompt preview">
                  <PromptPreview key={selectedPrompt?.id} prompt={selectedPrompt} names={names} />
                </aside>
              )}
            </div>
          </>
        )}
      </div>

      {sheetOpen && (
        <Dialog overlayClassName="prompt-modal-overlay prompts-sheet-overlay" className="prompts-preview prompts-sheet glass-panel" ariaLabel="Prompt preview" onClose={closeSheet}>
          <button type="button" className="btn btn-ghost btn-sm prompts-sheet-close" onClick={closeSheet}>Close preview</button>
          <PromptPreview key={chosen?.id} prompt={chosen} names={names} headingRef={sheetHeadingRef} />
        </Dialog>
      )}

      {creating !== null && <CreatePromptPanel initialNamespace={creating} initialEnvironmentId={creatingEnvironmentId} onClose={() => { setCreating(null); setCreatingEnvironmentId('') }} />}
    </section>
  )
}

function PromptGroupSection({ group, children }: { group: PromptGroup; children: ReactNode }) {
  const headingId = useId()
  const count = group.rows.length
  return (
    <section className="prompts-group" aria-labelledby={group.title ? headingId : undefined}>
      {group.title && <h2 id={headingId} className="prompts-group-head">
        {group.title}
        <span className="sr-only">{`, ${count} ${count === 1 ? 'prompt' : 'prompts'}`}</span>
        <span className="prompts-group-count" aria-hidden="true">{count}</span>
      </h2>}
      {group.note && <p className="prompts-muted prompts-group-note">{group.note}</p>}
      <ul className="prompts-items">{children}</ul>
    </section>
  )
}
