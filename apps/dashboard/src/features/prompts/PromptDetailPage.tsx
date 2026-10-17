import { useTimestampPreference } from '@/theme/preferences'
import { Skeleton } from '@/components/ux/Skeleton'
import { TabList, TabPanels } from '@/components/ux/TabList'
import { Dialog } from '@/components/Dialog'
import { OverflowMenu } from '@/components/ux/OverflowMenu'
/**
 * /prompts/:ns/:name (?tab=content|versions) — prompt detail (plan UX 2.e).
 * Header: namespace/key breadcrumb, active vN chip, [Preview template],
 * [New version] and an archive overflow action (global admin only). Content tab: read-only active content + note +
 * description; new-version mode swaps in an editable editor pre-filled with
 * the active content, a note field, a line-diff-vs-active indicator and
 * [Save and activate vN+1]. Versions tab: newest-first timeline with per-version
 * [View] and [Set active] (rollback behind a confirm modal).
 */
import { useId, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'

import {
  promptWriteMutationKey,
  usePrompt,
  usePromptVersions,
  useRollbackPrompt,
  useSaveVersion,
  useSetArchived,
  type PromptDetail,
  type PromptVersionInfo,
} from '@/api/hooks/usePrompts'
import { usePendingMutationCount } from '@/api/hooks/usePendingMutationCount'
import { isApiError } from '@/api/errors'
import { RequireGlobalAdmin } from '@/auth/RequireRole'
import { CachedDataWarning } from '@/components/CachedDataWarning'
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
  maxVersion,
  save,
  content,
  note,
  onContentChange,
  onNoteChange,
  onDone,
  onCancel,
  writePending,
}: {
  detail: PromptDetail
  maxVersion: number
  save: ReturnType<typeof useSaveVersion>
  content: string
  note: string
  onContentChange: (content: string) => void
  onNoteChange: (note: string) => void
  onDone: (promptId: string) => void
  onCancel: () => void
  writePending: boolean
}) {
  const activeContent = detail.content ?? ''
  const nextVersion = maxVersion + 1
  const unchanged = content === activeContent

  function submit() {
    if (unchanged || writePending) return
    save.mutate(
      { content, ...(note.trim() ? { note: note.trim() } : {}) },
      { onSuccess: () => onDone(detail.id) },
    )
  }

  return (
    <div className="prompt-content-card glass-panel">
      <div className="prompt-editor-bar">
        <span className="strong">New version</span>
        <DiffIndicator active={activeContent} draft={content} />
      </div>
      <PromptEditor
        value={content}
        onChange={onContentChange}
        ariaLabel="New version content"
        disabled={writePending}
      />
      <label className="prompt-field">
        <span className="prompt-field-label">Version note</span>
        <input
          className="field-input"
          value={note}
          onChange={(event) => onNoteChange(event.target.value)}
          placeholder="why this version exists"
          aria-label="Version note"
          disabled={writePending}
        />
      </label>
      {save.isError && (
        <div className="tonal-card danger" role="alert">
          {errorMessage(save.error, 'Save failed.')}
        </div>
      )}
      <p className="tonal-card warning">Saving makes this the active catalog version immediately. New runs that follow the active version will use this content.</p>
      <div className="prompt-modal-actions">
        <button type="button" className="btn btn-ghost" onClick={onCancel} disabled={writePending}>
          Cancel
        </button>
        <button
          type="button"
          className="btn btn-primary"
          onClick={submit}
          disabled={unchanged || writePending}
        >
          {save.isPending ? 'Saving…' : `Save and activate v${nextVersion}`}
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
  const writeCount = usePendingMutationCount(promptWriteMutationKey(detail.id))
  const writePending = writeCount > 0
  const [confirming, setConfirming] = useState<PromptVersionInfo | null>(null)

  const versions = useMemo(
    () => [...(versionsQuery.data ?? [])].sort((a, b) => b.version - a.version),
    [versionsQuery.data],
  )

  if (versionsQuery.isPending) {
    return (
      <Skeleton label="Loading versions" rows={2} />
    )
  }
  if (versionsQuery.isError && !versionsQuery.data) {
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
      {versionsQuery.isError && (
        <CachedDataWarning
          error={versionsQuery.error}
          onRetry={() => void versionsQuery.refetch()}
        />
      )}
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
                    <RequireGlobalAdmin>
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        onClick={() => {
                          rollback.reset()
                          setConfirming(version)
                        }}
                        disabled={writePending}
                      >
                        Set active
                      </button>
                    </RequireGlobalAdmin>
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
          pending={writePending}
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
  useTimestampPreference()
  const { ns, name } = usePromptRouteParams()
  return <PromptDetailContent key={JSON.stringify([ns, name])} ns={ns} name={name} />
}

function PromptDetailContent({ ns, name }: { ns: string; name: string }) {
  const tabsId = useId()
  const [archiveConfirm, setArchiveConfirm] = useState(false)
  const [searchParams, setSearchParams] = useSearchParams()
  const tab: Tab = searchParams.get('tab') === 'versions' ? 'versions' : 'content'
  const [editing, setEditing] = useState(false)
  const [versionContent, setVersionContent] = useState('')
  const [versionNote, setVersionNote] = useState('')

  const detailQuery = usePrompt(ns, name)
  const detail = detailQuery.data
  const activePromptIdRef = useRef<string | null>(detail?.id ?? null)
  activePromptIdRef.current = detail?.id ?? null
  const setArchived = useSetArchived(ns, name, detail?.id)
  const versionsQuery = usePromptVersions(ns, name, detail?.id)
  const saveVersion = useSaveVersion(detail?.id)
  const writeCount = usePendingMutationCount(promptWriteMutationKey(detail?.id))
  const writePending = writeCount > 0

  function selectTab(next: Tab) {
    if (writePending) return
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
        <Skeleton label="Loading prompt" rows={2} />
      </section>
    )
  }
  if (!detail) {
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
      {detailQuery.isError && (
        <CachedDataWarning error={detailQuery.error} onRetry={() => void detailQuery.refetch()} />
      )}
      <header className="prompt-detail-header glass-panel">
        <div className="prompt-detail-title">
          <nav className="prompt-breadcrumb" aria-label="Breadcrumb">
            <Link to={`/prompts?ns=${encodeURIComponent(ns)}`}>{ns}</Link>
            <span aria-hidden="true"> / </span>
            <span className="strong">{detail.key}</span>
          </nav>
          <h2 className="prompt-detail-name">{detail.key}</h2>
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
            Preview template
          </Link>
          <RequireGlobalAdmin>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => {
                selectTab('content')
                saveVersion.reset()
                setVersionContent(detail.content ?? '')
                setVersionNote('')
                setEditing(true)
              }}
              disabled={editing || writePending}
            >
              New version
            </button>
            <OverflowMenu
              label={`Prompt actions: ${detail.key}`}
              items={[{
                label: archived ? 'Unarchive' : 'Archive…',
                disabled: writePending,
                onSelect: () => archived ? setArchived.mutate(false) : setArchiveConfirm(true),
              }]}
            />
          </RequireGlobalAdmin>
        </div>
      </header>
      {setArchived.isError && (
        <div className="tonal-card danger" role="alert">
          {errorMessage(setArchived.error, 'Archive toggle failed.')}
        </div>
      )}

      {archiveConfirm && <Dialog className="prompt-modal glass-panel" overlayClassName="prompt-modal-overlay" ariaLabel="Archive prompt?" onClose={() => setArchiveConfirm(false)}>
        <h2>Archive {detail.key}?</h2><p>This hides the prompt from the active catalog. Existing versions are retained and you can unarchive it later.</p>
        <div className="ux-actions"><button type="button" className="btn btn-secondary" onClick={() => setArchiveConfirm(false)}>Cancel</button><button type="button" className="btn btn-danger" disabled={writePending} onClick={() => { setArchiveConfirm(false); setArchived.mutate(true) }}>Confirm archive</button></div>
      </Dialog>}
      <TabList disabled={writePending} id={tabsId} label="Prompt sections" value={tab} items={[{ id: 'content', label: 'Content' }, { id: 'versions', label: 'Versions' }]} onChange={selectTab} />
      <TabPanels id={tabsId} items={['content', 'versions']} value={tab}>
      {tab === 'content' ? (
        editing ? (
          <NewVersionEditor
            key={detail.id}
            detail={detail}
            save={saveVersion}
            content={versionContent}
            note={versionNote}
            onContentChange={setVersionContent}
            onNoteChange={setVersionNote}
            maxVersion={Math.max(
              detail.active_version?.version ?? 0,
              ...(versionsQuery.data ?? []).map((entry) => entry.version),
            )}
            onDone={(savedPromptId) => {
              if (activePromptIdRef.current === savedPromptId) {
                setEditing(false)
                setVersionContent('')
                setVersionNote('')
              }
            }}
            onCancel={() => {
              setEditing(false)
              setVersionContent('')
              setVersionNote('')
            }}
            writePending={writePending}
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
        <VersionsTimeline key={detail.id} ns={ns} name={name} detail={detail} />
      )}
      </TabPanels>
    </section>
  )
}
