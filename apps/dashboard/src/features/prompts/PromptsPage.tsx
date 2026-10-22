import { PHASE_NAMES } from '@apex/pipeline-events'
import type { PhaseName } from '@apex/pipeline-events'
import { useApplicationsIndex, useEnvironmentsIndex } from '@/api/hooks/useEnvironments'
import { PROMPT_CATEGORIES, PROMPT_LABELS } from './promptCategories'
import { useTimestampPreference } from '@/theme/preferences'
import { StateCard } from '@/components/ux/StateCard'
import { Skeleton } from '@/components/ux/Skeleton'
/**
 * /prompts — catalog browser (plan UX 2.e). Left namespace tree (distinct
 * namespaces with counts, 'phase' pinned first), main .data-table with key /
 * description / active vN / updated-relative, include-archived toggle and
 * debounced ?q search (both server-side), ?ns namespace filter client-side.
 * [New prompt] (operator+) opens a create panel -> POST -> navigate detail.
 */
import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'

import {
  useCreatePrompt,
  useMakeDefaultPrompt,
  usePromptList,
  usePromptById,
  type PromptSummary,
} from '@/api/hooks/usePrompts'
import { isApiError } from '@/api/errors'
import { RequireGlobalAdmin } from '@/auth/RequireRole'
import { CodeViewer } from '@/components/viewers/CodeViewer'
import { Dialog } from '@/components/Dialog'
import { ProblemCard } from '@/components/ProblemCard'
import { formatRelative } from '@/utils/time'

import { PromptEditor } from './PromptEditor'
import { promptPath } from './promptPaths'
import './prompts.css'

const SEARCH_DEBOUNCE_MS = 300
const EM_DASH = '—'

function errorMessage(error: unknown, fallback: string): string {
  if (isApiError(error)) return error.message
  if (error instanceof Error) return error.message
  return fallback
}

/** Distinct namespaces with counts; 'phase' first, the rest alphabetical. */
function namespaceIndex(rows: PromptSummary[]): Array<{ ns: string; count: number }> {
  return PROMPT_CATEGORIES.map(ns => ({ ns, count: rows.filter(row => row.namespace === ns && row.selection_enabled).length }))
}

interface PromptGroup {
  id: string
  label: string
  title: string
  rows: PromptSummary[]
}

function readableName(value: string): string {
  const words = value.replaceAll(/[_-]+/g, ' ').trim()
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : value
}

function namespaceLabel(namespace: string): string {
  return PROMPT_LABELS[namespace] ?? namespace
}

function promptGroups(rows: PromptSummary[], namespace: string, names: Record<string, string>): PromptGroup[] | null {
  if (!namespace) return [
    { id: 'selectable', label: 'Prompt catalog', title: 'Available selections', rows: rows.filter(row => row.selection_enabled) },
    { id: 'compatibility', label: 'Compatibility', title: 'Legacy templates and unresolved entries', rows: rows.filter(row => !row.selection_enabled) },
  ].filter(group => group.rows.length)
  if (namespace === 'system') return null
  const grouped = new Map<string, PromptSummary[]>()
  for (const row of rows) {
    const id = namespace === 'phase' ? row.phase ?? 'Unassigned' : namespace === 'application' ? row.is_shared ? 'Shared' : row.application_id ?? 'Unassigned' : row.environment_id ?? 'Unassigned'
    grouped.set(id, [...(grouped.get(id) ?? []), row])
  }
  return [...grouped.entries()].sort(([a], [b]) => namespace === 'phase' ? PHASE_NAMES.indexOf(a as PhaseName) - PHASE_NAMES.indexOf(b as PhaseName) : a.localeCompare(b)).map(([id, groupRows]) => ({ id, label: namespaceLabel(namespace), title: names[id] ?? readableName(id), rows: groupRows }))
}

function PromptRow({
  prompt,
  selected,
  onSelect,
}: {
  prompt: PromptSummary
  selected: boolean
  onSelect: () => void
}) {
  const archived = Boolean(prompt.archived_at)
  return (
    <tr
      className={`prompts-row${archived ? ' prompts-row-archived' : ''}${selected ? ' prompts-row-selected' : ''}`}
      data-testid={`prompt-row-${prompt.id}`}
    >
      <td>
        <button
          type="button"
          className="strong prompts-key"
          aria-pressed={selected}
          onClick={onSelect}
        >
          {prompt.key}
        </button>
      </td>
      <td>{namespaceLabel(prompt.namespace)}{prompt.is_shared ? ' · Shared' : ''}</td>
      <td className="prompts-description">{prompt.description || EM_DASH}</td>
      <td>
        <div className="prompts-badges">
          {prompt.active_version ? (
            <span className="dash-context-chip prompts-version-chip">
              v{prompt.active_version.version}
            </span>
          ) : (
            <span className="prompts-muted">{EM_DASH}</span>
          )}
          {prompt.is_default && <span className="topbar-meta-chip">default</span>}
          {archived && <span className="topbar-meta-chip prompts-archived-chip">archived</span>}
        </div>
      </td>
      <td className="prompts-time" title={prompt.updated_at ?? undefined}>
        {formatRelative(prompt.updated_at)}
      </td>
    </tr>
  )
}

function PromptPreview({ prompt }: { prompt: PromptSummary | undefined }) {
  const makeDefault = useMakeDefaultPrompt()
  const detailQuery = usePromptById(prompt?.namespace ?? '', prompt?.key ?? '', prompt?.id)
  if (!prompt) {
    return (
      <aside className="prompts-preview glass-panel" aria-label="Prompt preview">
        <h2>Select a prompt</h2>
        <p className="prompts-muted">Choose a key to read its active prompt without leaving the catalog.</p>
      </aside>
    )
  }
  if (detailQuery.isPending) {
    return (
      <aside className="prompts-preview glass-panel" aria-label="Prompt preview" aria-busy="true">
        <Skeleton label="Loading active prompt" rows={2} />
      </aside>
    )
  }
  if (!detailQuery.data) {
    return (
      <aside className="prompts-preview glass-panel" aria-label="Prompt preview">
        <h2>Prompt unavailable</h2>
        <p className="prompts-muted">{errorMessage(detailQuery.error, 'The active prompt could not be loaded.')}</p>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => void detailQuery.refetch()}>
          Retry
        </button>
      </aside>
    )
  }
  const detail = detailQuery.data
  return (
    <aside className="prompts-preview glass-panel" aria-label="Prompt preview">
      <div className="prompts-preview-header">
        <div>
          <p className="prompts-preview-kicker">
            {namespaceLabel(detail.namespace)}
          </p>
          <h2>{detail.description || readableName(detail.key)}</h2>
          <code className="prompts-key-meta">{detail.key}</code>
        </div>
        <div className="prompts-preview-meta">
          {detail.active_version && <span className="dash-context-chip prompts-version-chip">Active v{detail.active_version.version}</span>}
          {detail.is_default && <span className="dash-context-chip">Default</span>}
          {detail.archived_at && <span className="topbar-meta-chip prompts-archived-chip">archived</span>}
          <span className="prompts-muted" title={detail.updated_at ?? undefined}>{formatRelative(detail.updated_at)}</span>
        </div>
      </div>
      {detail.description && <p className="prompt-description">{detail.description}</p>}
      {detail.content ? (
        <CodeViewer value={detail.content} ariaLabel={`Active prompt content for ${detail.key}`} />
      ) : (
        <StateCard kind="empty" compact title="No active prompt content." action={null} />
      )}
      <div className="prompts-preview-footer">
        {detail.note ? <span className="prompts-muted">{detail.note}</span> : <span />}
        {detail.selection_enabled && !detail.archived_at && !detail.is_default && ['system', 'phase'].includes(detail.namespace) && <RequireGlobalAdmin><button type="button" className="btn btn-secondary btn-sm" disabled={makeDefault.isPending} onClick={() => makeDefault.mutate(detail.id)}>Make default</button></RequireGlobalAdmin>}
        {makeDefault.isError && <p role="alert">{makeDefault.error.message}</p>}
        <Link className="btn btn-secondary btn-sm" to={promptPath(detail.namespace, detail.key)}>
          Open full editor
        </Link>
      </div>
    </aside>
  )
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
}

const EMPTY_DRAFT: CreateDraft = { namespace: 'system', phase: null, application_id: '', environment_id: '', is_shared: false, is_default: false, key: '', description: '', content: '', note: '' }

function CreatePromptPanel({
  onClose,
}: {
  onClose: () => void
}) {
  const navigate = useNavigate()
  const create = useCreatePrompt()
  const applications = useApplicationsIndex()
  const environments = useEnvironmentsIndex()
  const [draft, setDraft] = useState<CreateDraft>(EMPTY_DRAFT)

  const valid =
    draft.namespace.trim().length > 0 && draft.key.trim().length > 0 && draft.content.trim().length > 0 &&
    (draft.namespace !== 'phase' || Boolean(draft.phase)) &&
    (draft.namespace !== 'application' || draft.is_shared || Boolean(draft.application_id)) &&
    (draft.namespace !== 'environment' || Boolean(draft.environment_id))

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
      <p>Creating this prompt makes version 1 available for selection. Existing tests keep their selected version.</p>
        <div className="prompt-form-grid">
          <label className="prompt-field">
            <span className="prompt-field-label">Category</span>
            <select className="field-input" value={draft.namespace} aria-label="Category" onChange={event => setDraft({ ...draft, namespace: event.target.value })}>
              {PROMPT_CATEGORIES.map(ns => <option key={ns} value={ns}>{namespaceLabel(ns)}</option>)}
            </select>
          </label>
          {draft.namespace === 'phase' && <label>Phase<select className="field-input" aria-label="Phase" value={draft.phase ?? ''} onChange={event => setDraft({ ...draft, phase: event.target.value as PhaseName })}><option value="">Choose a phase</option>{PHASE_NAMES.map(phase => <option key={phase} value={phase}>{readableName(phase)}</option>)}</select></label>}
          {draft.namespace === 'application' && <>
            <label>Applicability<select className="field-input" aria-label="Application applicability" value={draft.is_shared ? 'shared' : 'application'} onChange={event => setDraft({ ...draft, is_shared: event.target.value === 'shared' })}><option value="application">Application-specific</option><option value="shared">Shared</option></select></label>
            {!draft.is_shared && <label>Application<select className="field-input" aria-label="Application" value={draft.application_id} onChange={event => setDraft({ ...draft, application_id: event.target.value })}><option value="">Choose an application</option>{applications.data?.map(app => <option key={app.id} value={app.id}>{app.name}</option>)}</select></label>}
          </>}
          {draft.namespace === 'environment' && <label>Environment<select className="field-input" aria-label="Environment" value={draft.environment_id} onChange={event => setDraft({ ...draft, environment_id: event.target.value })}><option value="">Choose an environment</option>{environments.data?.map(env => <option key={env.id} value={env.id}>{applications.data?.find(app => app.id === env.application_id)?.name ?? env.application_id} / {env.name}</option>)}</select></label>}
          {['system', 'phase'].includes(draft.namespace) && <label><input type="checkbox" checked={draft.is_default} onChange={event => setDraft({ ...draft, is_default: event.target.checked })} /> Make default</label>}
          <label className="prompt-field">
            <span className="prompt-field-label">Key</span>
            <input
              className="field-input"
              value={draft.key}
              onChange={(event) => setDraft({ ...draft, key: event.target.value })}
              placeholder="checkout-analysis"
              aria-label="Key"
            />
          </label>
        </div>
        <label className="prompt-field">
          <span className="prompt-field-label">Description</span>
          <input
            className="field-input"
            value={draft.description}
            onChange={(event) => setDraft({ ...draft, description: event.target.value })}
            aria-label="Description"
          />
        </label>
        <div className="prompt-field">
          <span className="prompt-field-label">Content</span>
          <PromptEditor
            value={draft.content}
            onChange={(content) => setDraft((prev) => ({ ...prev, content }))}
            ariaLabel="Prompt content"
          />
        </div>
        <label className="prompt-field">
          <span className="prompt-field-label">Version note</span>
          <input
            className="field-input"
            value={draft.note}
            onChange={(event) => setDraft({ ...draft, note: event.target.value })}
            placeholder="why this version exists"
            aria-label="Version note"
          />
        </label>
        {create.isError && (
          <div className="tonal-card danger" role="alert">
            {errorMessage(create.error, 'Create failed.')}
          </div>
        )}
        <div className="prompt-modal-actions">
          <button
            type="button"
            className="btn btn-ghost"
            onClick={onClose}
            disabled={create.isPending}
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={submit}
            disabled={!valid || create.isPending}
          >
            {create.isPending ? 'Creating…' : 'Create and activate prompt'}
          </button>
        </div>
    </Dialog>
  )
}

export function PromptsPage() {
  useTimestampPreference()
  const [searchParams, setSearchParams] = useSearchParams()
  const selectedNs = searchParams.get('ns') ?? ''
  const selectedId = searchParams.get('prompt') ?? ''
  const includeArchived = searchParams.get('archived') === '1'
  const committedQ = searchParams.get('q') ?? ''
  const [search, setSearch] = useState(committedQ)
  const [creating, setCreating] = useState(false)

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

  const rows = useMemo(() => {
    const all = [...(data ?? [])].sort(
      (a, b) => a.namespace.localeCompare(b.namespace) || a.key.localeCompare(b.key),
    )
    return selectedNs ? all.filter((row) => row.namespace === selectedNs && row.selection_enabled) : all
  }, [data, selectedNs])
  const tree = useMemo(() => namespaceIndex(data ?? []), [data])
  const applications = useApplicationsIndex()
  const environments = useEnvironmentsIndex()
  const associationNames = useMemo(() => {
    const names: Record<string, string> = {}
    for (const app of applications.data ?? []) names[app.id] = app.name
    for (const env of environments.data ?? []) names[env.id] = `${names[env.application_id] ?? env.application_id} / ${env.name}`
    return names
  }, [applications.data, environments.data])
  const groups = useMemo(() => promptGroups(rows, selectedNs, associationNames), [rows, selectedNs, associationNames])
  const total = data?.length ?? 0
  const selectedPrompt = rows.find((row) => row.id === selectedId) ?? rows[0]

  function selectNamespace(ns: string) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      if (ns) next.set('ns', ns)
      else next.delete('ns')
      next.delete('prompt')
      return next
    })
  }

  function toggleArchived(checked: boolean) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      if (checked) next.set('archived', '1')
      else next.delete('archived')
      next.delete('prompt')
      return next
    })
  }

  function selectPrompt(promptId: string) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.set('prompt', promptId)
      return next
    })
  }

  return (
    <section className="prompts-page animate-enter">
      <header className="prompts-toolbar glass-panel">
        <input
          type="search"
          className="field-input prompts-search"
          placeholder="Search prompts…"
          aria-label="Search prompts"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <label className="prompts-archived-toggle">
          <input
            type="checkbox"
            checked={includeArchived}
            onChange={(event) => toggleArchived(event.target.checked)}
          />
          Include archived
        </label>
        <RequireGlobalAdmin>
          <button
            type="button"
            className="btn btn-primary prompts-new-btn"
            onClick={() => setCreating(true)}
          >
            New prompt
          </button>
        </RequireGlobalAdmin>
      </header>

      <div className="prompts-layout">
        <nav className="prompts-tree glass-panel" aria-label="Namespaces">
          <button
            type="button"
            className={`prompts-tree-item${selectedNs === '' ? ' active' : ''}`}
            aria-current={selectedNs === '' ? 'true' : undefined}
            onClick={() => selectNamespace('')}
          >
            <span>All Namespaces</span>
            <span className="prompts-tree-count">{total}</span>
          </button>
          {tree.map(({ ns, count }) => (
            <button
              key={ns}
              type="button"
              className={`prompts-tree-item${selectedNs === ns ? ' active' : ''}`}
              aria-current={selectedNs === ns ? 'true' : undefined}
              onClick={() => selectNamespace(ns)}
            >
              <span>{namespaceLabel(ns)}</span>
              <span className="prompts-tree-count">{count}</span>
            </button>
          ))}
        </nav>

        <div className="prompts-main">
          {isError && (!data || isPlaceholderData) ? (
            <ProblemCard
              title="Prompt catalog unavailable"
              message={errorMessage(error, 'The prompt list could not be loaded.')}
              onRetry={() => refetch()}
            />
          ) : isPending || isPlaceholderData ? (
            <Skeleton label="Loading prompts" rows={5} />
          ) : rows.length === 0 ? (
            <StateCard kind={committedQ || selectedNs ? 'filtered' : 'empty'} title="No prompts found" action={committedQ || selectedNs
              ? <button className="btn btn-secondary" onClick={() => { setSearch(''); setSearchParams({}) }}>Clear filters</button>
              : <RequireGlobalAdmin><button className="btn btn-primary" onClick={() => setCreating(true)}>Create prompt</button></RequireGlobalAdmin>}>
              {committedQ || selectedNs ? 'No prompts match the current filters.' : 'Create the first prompt to seed the catalog.'}
            </StateCard>
          ) : (
            <>
              {isError && (
                <div className="runs-refresh-error" role="alert">
                  <span>Refresh failed: {errorMessage(error, 'The prompt list could not be loaded.')}</span>
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => refetch()}>
                    Retry
                  </button>
                </div>
              )}
              <div className="prompts-browser">
                <div className="data-table-wrap">
                  <table className="data-table striped prompts-table">
                <thead>
                  <tr>
                    <th>Key</th>
                    <th>Category</th>
                    <th>Description</th>
                    <th>Active</th>
                    <th>Updated</th>
                  </tr>
                </thead>
                {groups ? (
                  groups.map((group) => (
                    <tbody key={group.id} className="prompts-table-group">
                      <tr className="prompts-group-heading">
                        <th
                          colSpan={5}
                          scope="rowgroup"
                          aria-label={`${group.label}: ${group.title}, ${group.rows.length} ${group.rows.length === 1 ? 'prompt' : 'prompts'}`}
                        >
                          <span className="prompts-group-label">{group.label}</span>
                          <span className="prompts-group-title">{group.title}</span>
                          <span className="prompts-group-count">
                            {group.rows.length} {group.rows.length === 1 ? 'prompt' : 'prompts'}
                          </span>
                        </th>
                      </tr>
                      {group.rows.map((prompt) => (
                        <PromptRow
                          key={prompt.id}
                          prompt={prompt}
                          selected={selectedPrompt?.id === prompt.id}
                          onSelect={() => selectPrompt(prompt.id)}
                        />
                      ))}
                    </tbody>
                  ))
                ) : (
                  <tbody>
                    {rows.map((prompt) => (
                      <PromptRow
                        key={prompt.id}
                        prompt={prompt}
                        selected={selectedPrompt?.id === prompt.id}
                        onSelect={() => selectPrompt(prompt.id)}
                      />
                    ))}
                  </tbody>
                )}
                  </table>
                </div>
                <PromptPreview prompt={selectedPrompt} />
              </div>
            </>
          )}
        </div>
      </div>

      {creating && (
        <CreatePromptPanel
          onClose={() => setCreating(false)}
        />
      )}
    </section>
  )
}
