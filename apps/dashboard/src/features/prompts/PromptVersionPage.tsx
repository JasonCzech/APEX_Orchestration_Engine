/**
 * /prompts/:ns/:name/versions/:v (?diff=<other_version_id>) — one immutable
 * version (plan UX 2.e). Plain CodeViewer + meta by default; picking another
 * version from the history dropdown writes ?diff= and swaps in the unified
 * @codemirror/merge view (comparison base = the ?diff version, document = the
 * page's version). [Set this version active] runs the rollback behind the
 * shared confirm modal (operator+).
 */
import { useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'

import {
  usePrompt,
  usePromptVersion,
  usePromptVersions,
  useRollbackPrompt,
} from '@/api/hooks/usePrompts'
import { isApiError } from '@/api/errors'
import { RequireGlobalAdmin } from '@/auth/RequireRole'
import { ProblemCard } from '@/components/ProblemCard'
import { CodeViewer } from '@/components/viewers/CodeViewer'
import { formatRelative } from '@/utils/time'

import { PromptDiff } from './PromptDiff'
import { promptPath, usePromptRouteParams } from './promptPaths'
import { RollbackConfirm } from './RollbackConfirm'
import './prompts.css'

function errorMessage(error: unknown, fallback: string): string {
  if (isApiError(error)) return error.message
  if (error instanceof Error) return error.message
  return fallback
}

export function PromptVersionPage() {
  const { ns, name } = usePromptRouteParams()
  const { v: versionId = '' } = useParams<{ v: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const diffId = searchParams.get('diff') ?? ''
  const [confirming, setConfirming] = useState(false)

  const detailQuery = usePrompt(ns, name)
  const detail = detailQuery.data
  const versionQuery = usePromptVersion(ns, name, versionId, detail?.id)
  const versionsQuery = usePromptVersions(ns, name, detail?.id)
  const diffQuery = usePromptVersion(ns, name, diffId || undefined, detail?.id)
  const rollback = useRollbackPrompt(ns, name, detail?.id)

  const history = useMemo(
    () => [...(versionsQuery.data ?? [])].sort((a, b) => b.version - a.version),
    [versionsQuery.data],
  )

  function setDiff(next: string) {
    setSearchParams((prev) => {
      const params = new URLSearchParams(prev)
      if (next) params.set('diff', next)
      else params.delete('diff')
      return params
    })
  }

  if (detailQuery.isPending || (detail && versionQuery.isPending)) {
    return (
      <section className="prompts-page animate-enter">
        <div role="status" aria-busy="true" aria-label="Loading version" className="prompts-muted">
          Loading version…
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
  if (versionQuery.isError || !versionQuery.data) {
    return (
      <section className="prompts-page animate-enter">
        <ProblemCard
          title="Version unavailable"
          message={errorMessage(versionQuery.error, 'The version could not be loaded.')}
          onRetry={() => versionQuery.refetch()}
        />
      </section>
    )
  }

  const version = versionQuery.data
  const isActive = detail.active_version?.id === version.id
  const diffVersion = history.find((entry) => entry.id === diffId)

  return (
    <section className="prompts-page animate-enter">
      <header className="prompt-detail-header glass-panel">
        <div className="prompt-detail-title">
          <nav className="prompt-breadcrumb" aria-label="Breadcrumb">
            <Link to={`/prompts?ns=${encodeURIComponent(ns)}`}>{ns}</Link>
            <span aria-hidden="true"> / </span>
            <Link to={promptPath(ns, name)}>{detail.key}</Link>
            <span aria-hidden="true"> / </span>
            <span className="strong">v{version.version}</span>
          </nav>
          <div className="prompts-badges">
            {isActive && <span className="status-badge success">active</span>}
            <span className="prompts-muted">
              {version.created_by ?? 'unknown'} · {formatRelative(version.created_at)}
            </span>
          </div>
        </div>
        <div className="prompt-detail-actions">
          {!isActive && (
            <RequireGlobalAdmin>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => {
                  rollback.reset()
                  setConfirming(true)
                }}
              >
                Set this version active
              </button>
            </RequireGlobalAdmin>
          )}
        </div>
      </header>

      <div className="prompt-content-card glass-panel">
        <div className="prompt-editor-bar">
          {version.note ? (
            <span className="prompts-muted">
              Note: <em>{version.note}</em>
            </span>
          ) : (
            <span className="prompts-muted">No version note</span>
          )}
          <label className="prompt-diff-picker">
            <span className="prompt-field-label">Compare with</span>
            <select
              className="field-select"
              aria-label="Compare with version"
              value={diffVersion ? diffId : ''}
              onChange={(event) => setDiff(event.target.value)}
            >
              <option value="">No comparison</option>
              {history
                .filter((entry) => entry.id !== version.id)
                .map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    v{entry.version}
                    {detail.active_version?.id === entry.id ? ' (active)' : ''}
                  </option>
                ))}
            </select>
          </label>
        </div>

        {diffVersion ? (
          diffQuery.isPending ? (
            <div role="status" aria-busy="true" aria-label="Loading comparison" className="prompts-muted">
              Loading comparison…
            </div>
          ) : diffQuery.isError || !diffQuery.data ? (
            <ProblemCard
              title="Comparison unavailable"
              message={errorMessage(diffQuery.error, 'The comparison version could not be loaded.')}
              onRetry={() => diffQuery.refetch()}
            />
          ) : (
            <>
              <p className="prompts-muted prompt-diff-legend">
                Unified diff — <span className="prompt-diff-removed">removed in v{version.version}</span>{' '}
                / <span className="prompt-diff-added">added in v{version.version}</span> relative to v
                {diffVersion.version}.
              </p>
              <PromptDiff
                original={diffQuery.data.content}
                value={version.content}
                ariaLabel={`Diff of v${version.version} against v${diffVersion.version}`}
              />
            </>
          )
        ) : (
          <CodeViewer value={version.content} ariaLabel={`Content of v${version.version}`} />
        )}
      </div>

      {confirming && (
        <RollbackConfirm
          version={version.version}
          note={version.note}
          pending={rollback.isPending}
          error={rollback.error ?? undefined}
          onCancel={() => setConfirming(false)}
          onConfirm={() => rollback.mutate(version.id, { onSuccess: () => setConfirming(false) })}
        />
      )}
    </section>
  )
}
