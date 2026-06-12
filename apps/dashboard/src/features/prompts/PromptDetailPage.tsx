/**
 * /prompts/:ns/:name (?tab=content|versions) — prompt detail (plan UX 2.e).
 * Header: namespace/key breadcrumb, active vN chip, [Test in playground],
 * [Archive|Unarchive] (operator+, optimistic with revert-on-error),
 * [New version] (operator+). Content tab: read-only active content + note +
 * description; new-version mode swaps in an editable editor pre-filled with
 * the active content, a note field, a line-diff-vs-active indicator and
 * [Save as vN+1]. Versions tab: newest-first timeline with per-version
 * [View] and [Set active] (rollback behind a confirm modal).
 */
import { useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router'

import {
  usePrompt,
  usePromptVersions,
  useRollbackPrompt,
  useSaveVersion,
  useSetArchived,
  type PromptDetail,
  type PromptVersionInfo,
} from '@/api/hooks/usePrompts'
import { isApiError } from '@/api/errors'
import { RequireRole } from '@/auth/RequireRole'
import { ProblemCard } from '@/components/ProblemCard'
import { CodeViewer } from '@/components/viewers/CodeViewer'
import { formatRelative } from '@/utils/time'

import { lineDiffStats } from './lineDiff'
import { PromptEditor } from './PromptEditor'
import { promptPlaygroundPath, promptVersionPath, usePromptRouteParams } from './promptPaths'
import { RollbackConfirm } from './RollbackConfirm'
import './prompts.css'

type Tab = 'content' | 'versions'

function errorMessage(error: unknown, fallback: string): string {
  if (isApiError(error)) return error.message
  if (error instanceof Error) return error.message
  return fallback
}

function DiffIndicator({ active, draft }: { active: string; draft: string }) {
  const stats = useMemo(() => lineDiffStats(active, draft), [active, draft])
  if (stats.added === 0 && stats.removed === 0) {
    return <span className="prompts-muted prompt-diff-indicator">No changes vs active</span>
  }
  return (
    <span className="prompt-diff-indicator" aria-label="Changes vs active">
      <span className="prompt-diff-added">+{stats.added}</span>{' '}
      <span className="prompt-diff-removed">−{stats.removed}</span> lines vs active
      {stats.truncated ? ' (approx.)' : ''}
    </span>
  )
}

function NewVersionEditor({
  detail,
  onDone,
  onCancel,
}: {
  detail: PromptDetail
  onDone: () => void
  onCancel: () => void
}) {
  const save = useSaveVersion(detail.id)
  const activeContent = detail.content ?? ''
  const [content, setContent] = useState(activeContent)
  const [note, setNote] = useState('')
  const nextVersion = (detail.active_version?.version ?? 0) + 1
  const unchanged = content === activeContent

  function submit() {
    if (unchanged || save.isPending) return
    save.mutate(
      { content, ...(note.trim() ? { note: note.trim() } : {}) },
      { onSuccess: onDone },
    )
  }

  return (
    <div className="prompt-content-card glass-panel">
      <div className="prompt-editor-bar">
        <span className="strong">New version</span>
        <DiffIndicator active={activeContent} draft={content} />
      </div>
      <PromptEditor value={content} onChange={setContent} ariaLabel="New version content" />
      <label className="prompt-field">
        <span className="prompt-field-label">Version note</span>
        <input
          className="field-input"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="why this version exists"
          aria-label="Version note"
        />
      </label>
      {save.isError && (
        <div className="tonal-card danger" role="alert">
          {errorMessage(save.error, 'Save failed.')}
        </div>
      )}
      <div className="prompt-modal-actions">
        <button type="button" className="btn btn-ghost" onClick={onCancel} disabled={save.isPending}>
          Cancel
        </button>
        <button
          type="button"
          className="btn btn-primary"
          onClick={submit}
          disabled={unchanged || save.isPending}
        >
          {save.isPending ? 'Saving…' : `Save as v${nextVersion}`}
        </button>
      </div>
    </div>
  )
}

function VersionsTimeline({
  ns,
  name,
  detail,
}: {
  ns: string
  name: string
  detail: PromptDetail
}) {
  const versionsQuery = usePromptVersions(ns, name, detail.id)
  const rollback = useRollbackPrompt(ns, name, detail.id)
  const [confirming, setConfirming] = useState<PromptVersionInfo | null>(null)

  const versions = useMemo(
    () => [...(versionsQuery.data ?? [])].sort((a, b) => b.version - a.version),
    [versionsQuery.data],
  )

  if (versionsQuery.isPending) {
    return (
      <div role="status" aria-busy="true" aria-label="Loading versions" className="prompts-muted">
        Loading versions…
      </div>
    )
  }
  if (versionsQuery.isError) {
    return (
      <ProblemCard
        title="Version history unavailable"
        message={errorMessage(versionsQuery.error, 'Versions could not be loaded.')}
        onRetry={() => versionsQuery.refetch()}
      />
    )
  }

  return (
    <>
      <ol className="prompt-timeline">
        {versions.map((version) => {
          const isActive = detail.active_version?.id === version.id
          return (
            <li
              key={version.id}
              className={`prompt-timeline-item${isActive ? ' active' : ''}`}
              data-testid={`prompt-version-${version.id}`}
            >
              <div className="prompt-timeline-marker" aria-hidden="true" />
              <div className="prompt-timeline-body glass-panel">
                <div className="prompt-timeline-head">
                  <span className="strong">v{version.version}</span>
                  {isActive && <span className="status-badge success">active</span>}
                  <span className="prompts-muted">
                    {version.created_by ?? 'unknown'} · {formatRelative(version.created_at)}
                  </span>
                </div>
                {version.note && <p className="prompt-timeline-note">{version.note}</p>}
                <div className="prompt-timeline-actions">
                  <Link className="btn btn-ghost btn-sm" to={promptVersionPath(ns, name, version.id)}>
                    View
                  </Link>
                  {!isActive && (
                    <RequireRole role="operator">
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        onClick={() => {
                          rollback.reset()
                          setConfirming(version)
                        }}
                      >
                        Set active
                      </button>
                    </RequireRole>
                  )}
                </div>
              </div>
            </li>
          )
        })}
      </ol>
      {confirming && (
        <RollbackConfirm
          version={confirming.version}
          note={confirming.note}
          pending={rollback.isPending}
          error={rollback.error ?? undefined}
          onCancel={() => setConfirming(null)}
          onConfirm={() =>
            rollback.mutate(confirming.id, { onSuccess: () => setConfirming(null) })
          }
        />
      )}
    </>
  )
}

export function PromptDetailPage() {
  const { ns, name } = usePromptRouteParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const tab: Tab = searchParams.get('tab') === 'versions' ? 'versions' : 'content'
  const [editing, setEditing] = useState(false)

  const detailQuery = usePrompt(ns, name)
  const detail = detailQuery.data
  const setArchived = useSetArchived(ns, name, detail?.id)

  function selectTab(next: Tab) {
    setSearchParams((prev) => {
      const params = new URLSearchParams(prev)
      if (next === 'content') params.delete('tab')
      else params.set('tab', next)
      return params
    })
  }

  if (detailQuery.isPending) {
    return (
      <section className="prompts-page animate-enter">
        <div role="status" aria-busy="true" aria-label="Loading prompt" className="prompts-muted">
          Loading prompt…
        </div>
      </section>
    )
  }
  if (detailQuery.isError || !detail) {
    return (
      <section className="prompts-page animate-enter">
        <ProblemCard
          title="Prompt unavailable"
          message={errorMessage(detailQuery.error, 'The prompt could not be loaded.')}
          onRetry={() => detailQuery.refetch()}
        />
      </section>
    )
  }

  const archived = Boolean(detail.archived_at)

  return (
    <section className="prompts-page animate-enter">
      <header className="prompt-detail-header glass-panel">
        <div className="prompt-detail-title">
          <nav className="prompt-breadcrumb" aria-label="Breadcrumb">
            <Link to={`/prompts?ns=${encodeURIComponent(ns)}`}>{ns}</Link>
            <span aria-hidden="true"> / </span>
            <span className="strong">{detail.key}</span>
          </nav>
          <div className="prompts-badges">
            {detail.active_version && (
              <span className="dash-context-chip prompts-version-chip">
                active v{detail.active_version.version}
              </span>
            )}
            {archived && <span className="topbar-meta-chip prompts-archived-chip">archived</span>}
          </div>
        </div>
        <div className="prompt-detail-actions">
          <Link className="btn btn-secondary" to={promptPlaygroundPath(ns, name)}>
            Test in playground
          </Link>
          <RequireRole role="operator">
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => setArchived.mutate(!archived)}
              disabled={setArchived.isPending}
            >
              {archived ? 'Unarchive' : 'Archive'}
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => {
                selectTab('content')
                setEditing(true)
              }}
              disabled={editing}
            >
              New version
            </button>
          </RequireRole>
        </div>
      </header>
      {setArchived.isError && (
        <div className="tonal-card danger" role="alert">
          {errorMessage(setArchived.error, 'Archive toggle failed.')}
        </div>
      )}

      <div className="prompt-tabs" role="tablist" aria-label="Prompt sections">
        {(['content', 'versions'] as const).map((entry) => (
          <button
            key={entry}
            type="button"
            role="tab"
            aria-selected={tab === entry}
            className={`prompt-tab${tab === entry ? ' active' : ''}`}
            onClick={() => selectTab(entry)}
          >
            {entry === 'content' ? 'Content' : 'Versions'}
          </button>
        ))}
      </div>

      {tab === 'content' ? (
        editing ? (
          <NewVersionEditor
            detail={detail}
            onDone={() => setEditing(false)}
            onCancel={() => setEditing(false)}
          />
        ) : (
          <div className="prompt-content-card glass-panel">
            {detail.description && <p className="prompt-description">{detail.description}</p>}
            <CodeViewer value={detail.content ?? ''} ariaLabel="Active prompt content" />
            {detail.note && (
              <p className="prompts-muted prompt-note">
                Note: <em>{detail.note}</em>
              </p>
            )}
          </div>
        )
      ) : (
        <VersionsTimeline ns={ns} name={name} detail={detail} />
      )}
    </section>
  )
}
