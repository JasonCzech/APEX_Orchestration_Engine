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
  usePromptList,
  type PromptSummary,
} from '@/api/hooks/usePrompts'
import { isApiError } from '@/api/errors'
import { RequireRole } from '@/auth/RequireRole'
import { Dialog } from '@/components/Dialog'
import { ProblemCard } from '@/components/ProblemCard'
import { formatRelative } from '@/utils/time'

import { PromptEditor } from './PromptEditor'
import { promptPath } from './promptPaths'
import './prompts.css'

const SEARCH_DEBOUNCE_MS = 300
const PINNED_NAMESPACE = 'phase'
const EM_DASH = '—'

function errorMessage(error: unknown, fallback: string): string {
  if (isApiError(error)) return error.message
  if (error instanceof Error) return error.message
  return fallback
}

/** Distinct namespaces with counts; 'phase' first, the rest alphabetical. */
function namespaceIndex(rows: PromptSummary[]): Array<{ ns: string; count: number }> {
  const counts = new Map<string, number>()
  for (const row of rows) counts.set(row.namespace, (counts.get(row.namespace) ?? 0) + 1)
  return [...counts.entries()]
    .map(([ns, count]) => ({ ns, count }))
    .sort((a, b) => {
      if (a.ns === PINNED_NAMESPACE) return -1
      if (b.ns === PINNED_NAMESPACE) return 1
      return a.ns.localeCompare(b.ns)
    })
}

function PromptRow({ prompt }: { prompt: PromptSummary }) {
  const archived = Boolean(prompt.archived_at)
  const target = promptPath(prompt.namespace, prompt.key)
  return (
    <tr
      className={`prompts-row${archived ? ' prompts-row-archived' : ''}`}
      data-testid={`prompt-row-${prompt.id}`}
    >
      <td>
        <Link to={target} className="strong prompts-key">
          {prompt.key}
        </Link>
      </td>
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
          {archived && <span className="topbar-meta-chip prompts-archived-chip">archived</span>}
        </div>
      </td>
      <td className="prompts-time" title={prompt.updated_at ?? undefined}>
        {formatRelative(prompt.updated_at)}
      </td>
    </tr>
  )
}

interface CreateDraft {
  namespace: string
  key: string
  description: string
  content: string
  note: string
}

const EMPTY_DRAFT: CreateDraft = { namespace: '', key: '', description: '', content: '', note: '' }

function CreatePromptPanel({
  namespaces,
  onClose,
}: {
  namespaces: string[]
  onClose: () => void
}) {
  const navigate = useNavigate()
  const create = useCreatePrompt()
  const [draft, setDraft] = useState<CreateDraft>(EMPTY_DRAFT)

  const valid =
    draft.namespace.trim().length > 0 && draft.key.trim().length > 0 && draft.content.length > 0

  function submit() {
    if (!valid || create.isPending) return
    create.mutate(
      {
        namespace: draft.namespace.trim(),
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
        <div className="prompt-form-grid">
          <label className="prompt-field">
            <span className="prompt-field-label">Namespace</span>
            <input
              className="field-input"
              list="prompt-namespaces"
              value={draft.namespace}
              onChange={(event) => setDraft({ ...draft, namespace: event.target.value })}
              placeholder="phase"
              aria-label="Namespace"
            />
            <datalist id="prompt-namespaces">
              {namespaces.map((ns) => (
                <option key={ns} value={ns} />
              ))}
            </datalist>
          </label>
          <label className="prompt-field">
            <span className="prompt-field-label">Key</span>
            <input
              className="field-input"
              value={draft.key}
              onChange={(event) => setDraft({ ...draft, key: event.target.value })}
              placeholder="story_analysis/system"
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
            {create.isPending ? 'Creating…' : 'Create prompt'}
          </button>
        </div>
    </Dialog>
  )
}

export function PromptsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const selectedNs = searchParams.get('ns') ?? ''
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
        return next
      })
    }, SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(id)
  }, [search, committedQ, setSearchParams])

  const { data, error, isPending, isError, refetch } = usePromptList({
    includeArchived,
    q: committedQ || undefined,
  })

  const rows = useMemo(() => {
    const all = [...(data ?? [])].sort(
      (a, b) => a.namespace.localeCompare(b.namespace) || a.key.localeCompare(b.key),
    )
    return selectedNs ? all.filter((row) => row.namespace === selectedNs) : all
  }, [data, selectedNs])
  const tree = useMemo(() => namespaceIndex(data ?? []), [data])
  const total = data?.length ?? 0

  function selectNamespace(ns: string) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      if (ns) next.set('ns', ns)
      else next.delete('ns')
      return next
    })
  }

  function toggleArchived(checked: boolean) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      if (checked) next.set('archived', '1')
      else next.delete('archived')
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
        <RequireRole role="operator">
          <button
            type="button"
            className="btn btn-primary prompts-new-btn"
            onClick={() => setCreating(true)}
          >
            New prompt
          </button>
        </RequireRole>
      </header>

      <div className="prompts-layout">
        <nav className="prompts-tree glass-panel" aria-label="Namespaces">
          <button
            type="button"
            className={`prompts-tree-item${selectedNs === '' ? ' active' : ''}`}
            aria-current={selectedNs === '' ? 'true' : undefined}
            onClick={() => selectNamespace('')}
          >
            <span>All namespaces</span>
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
              <span>{ns}</span>
              <span className="prompts-tree-count">{count}</span>
            </button>
          ))}
        </nav>

        <div className="prompts-main">
          {isPending ? (
            <div
              className="prompts-skeleton"
              role="status"
              aria-busy="true"
              aria-label="Loading prompts"
            >
              {Array.from({ length: 5 }, (_, i) => (
                <div key={i} className="glass-panel prompts-skeleton-row" />
              ))}
            </div>
          ) : isError && !data ? (
            <ProblemCard
              title="Prompt catalog unavailable"
              message={errorMessage(error, 'The prompt list could not be loaded.')}
              onRetry={() => refetch()}
            />
          ) : rows.length === 0 ? (
            <div className="dash-empty">
              <h2>No prompts found</h2>
              <p className="dash-empty-hint">
                {committedQ || selectedNs
                  ? 'No prompts match the current filters.'
                  : 'Create the first prompt to seed the catalog.'}
              </p>
            </div>
          ) : (
            <div className="data-table-wrap">
              <table className="data-table striped prompts-table">
                <thead>
                  <tr>
                    <th>Key</th>
                    <th>Description</th>
                    <th>Active</th>
                    <th>Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((prompt) => (
                    <PromptRow key={prompt.id} prompt={prompt} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {creating && (
        <CreatePromptPanel
          namespaces={tree.map((entry) => entry.ns)}
          onClose={() => setCreating(false)}
        />
      )}
    </section>
  )
}
