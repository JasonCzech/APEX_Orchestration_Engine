import { StateCard } from '@/components/ux/StateCard'
import { Skeleton } from '@/components/ux/Skeleton'
import { TableToolbar } from '@/components/ux/TableToolbar'
import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router'

import { useLogSearch, type LogEntry } from '@/api/hooks/useLogs'
import { isApiError } from '@/api/errors'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { ProblemCard } from '@/components/ProblemCard'
import { Pagination } from '@/components/ux/Pagination'
import { WindowPresets } from '@/components/controls/WindowPresets'
import { DAY_MS, HOUR_MS, type WindowPreset } from '@/components/controls/timeWindow'
import { formatRelative, formatTimestamp } from '@/utils/time'

import {
  buildLogSearchInput,
  hasLogsFilters,
  levelTone,
  LOG_LEVELS,
  LOGS_MAX_OFFSET,
  LOGS_PAGE_SIZE,
  parseLogsFilters,
  serializeLogsFilters,
  type LogsFilters,
} from './logsFilters'
import './logs.css'


/** Logs default to short windows (server default = last hour when unset). */
const LOG_WINDOW_PRESETS: WindowPreset[] = [
  { label: '1h', ms: HOUR_MS },
  { label: '24h', ms: DAY_MS },
  { label: '7d', ms: 7 * DAY_MS },
]

function LogRow({ entry }: { entry: LogEntry }) {
  const [open, setOpen] = useState(false)
  const fields = entry.fields ?? {}
  const hasFields = Object.keys(fields).length > 0

  return (
    <>
      <tr className="logs-row">
        <td className="log-time" title={formatRelative(entry.at)}>
          {formatTimestamp(entry.at)}
        </td>
        <td>
          <span className={`status-badge ${levelTone(entry.level)}`}>{entry.level}</span>
        </td>
        <td>
          <span className="dash-context-chip">{entry.service}</span>
        </td>
        <td className="log-message">{entry.message}</td>
        <td className="log-expand-cell">
          {hasFields && (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              aria-expanded={open}
              aria-label={`Toggle fields for entry at ${entry.at}`}
              onClick={() => setOpen((value) => !value)}
            >
              {open ? 'Hide' : 'Fields'}
            </button>
          )}
        </td>
      </tr>
      {open && (
        <tr className="log-fields-row" data-testid="log-fields-row">
          <td colSpan={5}>
            <pre className="log-fields-json">{JSON.stringify(fields, null, 2)}</pre>
          </td>
        </tr>
      )}
    </>
  )
}

function LogsSkeleton() {
  return <Skeleton label="Searching logs" rows={8} />
}

/**
 * /logs — log search over POST /v1/logs/search (plan Part 2).
 *
 * Submit-only by design: the endpoint is a POST search, so typing never fires
 * requests — the query runs only on explicit [Search] (which also commits the
 * form to ?q&from&to&thread&service&level for shareable links) or once on
 * mount when the URL already carries filters (run pages deep-link ?thread=).
 * Pagination re-runs the *submitted* search at a new offset.
 */
export function LogsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const committed = useMemo(() => parseLogsFilters(searchParams), [searchParams])
  const committedKey = serializeLogsFilters(committed).toString()

  // Pending form state (URL-prefilled, e.g. ?thread= deep links).
  const [form, setForm] = useState<LogsFilters>(committed)
  // The submitted search; null until the user submits (or a deep link mounts).
  const [submitted, setSubmitted] = useState(() =>
    hasLogsFilters(committed) ? buildLogSearchInput(committed) : null,
  )
  const [offset, setOffset] = useState(0)
  const [limit, setLimit] = useState(LOGS_PAGE_SIZE)

  useEffect(() => {
    // The URL is the committed-search source of truth. In particular, browser
    // Back/Forward must update both the visible controls and the query that
    // produced the results; updating only the form leaves stale results under
    // a different URL.
    const next = parseLogsFilters(new URLSearchParams(committedKey))
    setForm(next)
    setSubmitted(hasLogsFilters(next) ? buildLogSearchInput(next) : null)
    setOffset(0)
  }, [committedKey])

  const { data, error, isPending, isError, isPlaceholderData, refetch } = useLogSearch(
    submitted ? { ...submitted, offset, limit } : null,
  )

  const patchForm = (patch: Partial<LogsFilters>) => {
    setForm((prev) => ({ ...prev, ...patch }))
  }

  const onSubmit = (event: React.FormEvent) => {
    event.preventDefault()
    const next: LogsFilters = {
      ...(form.q?.trim() ? { q: form.q.trim() } : {}),
      ...(form.from ? { from: form.from } : {}),
      ...(form.to ? { to: form.to } : {}),
      ...(form.thread?.trim() ? { thread: form.thread.trim() } : {}),
      ...(form.service?.trim() ? { service: form.service.trim() } : {}),
      ...(form.level ? { level: form.level } : {}),
      ...(form.run ? { run: form.run } : {}),
      ...(form.returnTo ? { returnTo: form.returnTo } : {}),
    }
    setSearchParams(serializeLogsFilters(next))
    setOffset(0)
    setSubmitted(buildLogSearchInput(next))
  }

  const entries = data?.entries ?? []
  const total = data?.total
  const nextDisabled =
    offset >= LOGS_MAX_OFFSET ||
    (total !== undefined ? offset + limit >= total : entries.length < limit)
  const rangeCaption =
    entries.length > 0
      ? `${offset + 1}–${offset + entries.length}${total !== undefined ? ` of ${total}` : ''} entries`
      : 'No entries'
  const hasCurrentData = data !== undefined && !isPlaceholderData
  const reachedResultWindow =
    offset >= LOGS_MAX_OFFSET &&
    total !== undefined &&
    offset + entries.length < total

  const queryRejected = isError && isApiError(error) && error.status === 422
  const upstreamDown = isError && isApiError(error) && error.status === 502
  const paginationFooter =
    entries.length > 0 || offset > 0 ? (
      <>
        {reachedResultWindow && (
          <p className="logs-result-window-note" role="status">
            Reached the provider result-window limit. Narrow the search to inspect later matches.
          </p>
        )}
        <footer className="logs-pagination">
          <span className="logs-pagination-caption">{rangeCaption}</span>
          <Pagination label="Logs" offset={offset} limit={limit} hasNext={!nextDisabled} pending={isPlaceholderData || isPending}
            onChange={({ offset: nextOffset, limit: nextLimit }) => { setOffset(Math.min(LOGS_MAX_OFFSET, nextOffset)); setLimit(nextLimit) }} />
        </footer>
      </>
    ) : null

  return (
    <section className="logs-page animate-enter">
      <form className="logs-toolbar glass-panel" role="search" aria-label="Log search" onSubmit={onSubmit}>
        <TableToolbar label="Log query" className="logs-toolbar-row">
          <label className="logs-query-field">
            <span>Search query</span>
            <input
              type="search"
              className="field-input logs-query"
              placeholder="Lucene query_string syntax"
              aria-label="Log query"
              value={form.q ?? ''}
              onChange={(event) => patchForm({ q: event.target.value })}
            />
          </label>
          <button type="submit" className="btn btn-primary">
            Search
          </button>
        </TableToolbar>
        <TableToolbar label="Log filters" filterCount={[committed.q, committed.from || committed.to, committed.thread, committed.service, committed.level].filter(Boolean).length} onClear={() => { setForm({}); setSearchParams(new URLSearchParams()); setSubmitted(null); setOffset(0) }} className="logs-toolbar-row">
          <label className="logs-filter-field">
            <span>Time window</span>
            <WindowPresets
              value={{ ...(form.from ? { from: form.from } : {}), ...(form.to ? { to: form.to } : {}) }}
              onChange={(window) => patchForm({ from: window.from, to: window.to })}
              presets={LOG_WINDOW_PRESETS}
            />
          </label>
          <div className="level-chips" role="group" aria-label="Level filter">
            {LOG_LEVELS.map((level) => (
              <button
                key={level}
                type="button"
                className="level-chip"
                aria-pressed={form.level === level}
                onClick={() => patchForm({ level: form.level === level ? undefined : level })}
              >
                {level}
              </button>
            ))}
          </div>
          <label className="logs-filter-field">
            <span>Service</span>
            <input
              type="text"
              className="field-input logs-service"
              placeholder="Any service"
              aria-label="Service filter"
              value={form.service ?? ''}
              onChange={(event) => patchForm({ service: event.target.value })}
            />
          </label>
          <label className="logs-filter-field">
            <span>Run ID</span>
            <input
              type="text"
              className="field-input logs-thread"
              placeholder="Any run"
              aria-label="Run ID filter"
              value={form.thread ?? ''}
              onChange={(event) => patchForm({ thread: event.target.value })}
            />
          </label>
        </TableToolbar>
      </form>

      {form.thread && (
        <aside className="logs-context glass-panel" aria-label="Selected run context">
          <div>
            <span className="logs-context-label">Investigating run</span>
            <strong>{form.run ?? form.thread}</strong>
            {form.run && <code>{form.thread}</code>}
          </div>
          {form.returnTo && (
            <Link className="btn btn-ghost btn-sm" to={form.returnTo}>
              {form.returnTo.includes('/reviews/') ? 'Return to review' : 'Return to run'}
            </Link>
          )}
        </aside>
      )}

      {!submitted ? (
        <StateCard kind="empty" title="Search the logs" action={<button type="button" className="btn btn-secondary" onClick={() => document.querySelector<HTMLInputElement>('.logs-query')?.focus()}>Enter a query</button>}>
          <p>Set a window and filters, then press Search — nothing runs on keystrokes.</p>
        </StateCard>
      ) : queryRejected && !hasCurrentData ? (
        <>
          <section className="glass-panel logs-query-error" role="alert">
            <h2>Query rejected</h2>
            <p>{error.message}</p>
          </section>
          {paginationFooter}
        </>
      ) : upstreamDown && !hasCurrentData ? (
        <>
          <ProblemCard
            title="Log search connection problem"
            message={error.message}
            onRetry={() => refetch()}
          />
          {paginationFooter}
        </>
      ) : isError && !hasCurrentData ? (
        <>
          <ProblemCard
            title="Log search failed"
            message={error instanceof Error ? error.message : 'The log search could not be run.'}
            onRetry={() => refetch()}
          />
          {paginationFooter}
        </>
      ) : isPending || isPlaceholderData ? (
        <LogsSkeleton />
      ) : (
        <>
          {isError && (
            <CachedDataWarning error={error} onRetry={() => void refetch()} />
          )}
          {entries.length === 0 ? (
            <StateCard kind="filtered" title="No log entries in this window" action={<button type="button" className="btn btn-secondary" onClick={() => document.querySelector<HTMLInputElement>('.logs-query')?.focus()}>Adjust search</button>}>
              <p>Widen the time window or relax the filters.</p>
            </StateCard>
          ) : (
            <div className="data-table-wrap">
              <table className="data-table striped logs-table">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Level</th>
                    <th>Service</th>
                    <th>Message</th>
                    <th className="log-expand-cell">
                      <span className="sr-only">Fields</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {entries.map((entry, index) => (
                    <LogRow key={`${offset}-${index}-${entry.at}`} entry={entry} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {paginationFooter}
        </>
      )}
    </section>
  )
}
