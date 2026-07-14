/**
 * /work-items/:provider/:itemId — one tracker item (plan Part 2 route table).
 * The :provider segment is display context only; the fetch keys on the
 * decoded :itemId (= item key). Enrich (operator+) POSTs fields JSON + an
 * optional comment and swaps in the refreshed item the server returns.
 * A 404 renders the dash-empty 'Item not found' state, not a problem card.
 */
import { useState, type FormEvent } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'

import { useEnrichWorkItem, useWorkItem, type WorkItem } from '@/api/hooks/useWorkTracking'
import { isApiError } from '@/api/errors'
import { useConsumer } from '@/auth/AuthProvider'
import { roleAtLeast } from '@/auth/RequireRole'
import { Dialog } from '@/components/Dialog'
import { ProblemCard } from '@/components/ProblemCard'

import { ExternalLink, KindChip, StatusBadge } from './workItemsBits'
import { descriptionParagraphs, parseJsonObject } from './workItemsLogic'
import './work-items.css'

/** Fields JSON editor + comment textarea -> POST items/{key}/enrich (operator+). */
function EnrichModal({
  item,
  project,
  onClose,
}: {
  item: WorkItem
  project?: string
  onClose: () => void
}) {
  const enrich = useEnrichWorkItem()
  const [fieldsText, setFieldsText] = useState('{}')
  const [comment, setComment] = useState('')

  const fieldsParse = parseJsonObject(fieldsText)
  const canSubmit = fieldsParse.ok && !enrich.isPending

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSubmit || !fieldsParse.ok) return
    enrich.mutate(
      {
        key: item.key,
        body: { fields: fieldsParse.value, comment: comment.trim() || null },
        ...(project ? { project } : {}),
      },
      { onSuccess: onClose },
    )
  }

  return (
    <Dialog
      overlayClassName="wi-overlay"
      className="wi-modal glass-panel"
      ariaLabel={`Enrich ${item.key}`}
      onClose={onClose}
      closeOnBackdrop={!enrich.isPending}
      closeOnEscape={!enrich.isPending}
      panelAs="form"
      onSubmit={submit}
    >
      <h2 className="wi-modal-title">Enrich {item.key}</h2>
      <p className="wi-modal-caption">
        Pushes field values and an optional comment to the tracker item.
      </p>
        <label className="wi-field">
          <span className="wi-field-label">Fields (JSON)</span>
          <textarea
            className="field-input wi-json-input"
            aria-label="Fields JSON"
            rows={5}
            spellCheck={false}
            value={fieldsText}
            onChange={(event) => setFieldsText(event.target.value)}
          />
        </label>
        {!fieldsParse.ok && (
          <p className="wi-caption wi-caption--danger" role="alert">
            {fieldsParse.message}
          </p>
        )}
        <label className="wi-field">
          <span className="wi-field-label">Comment</span>
          <textarea
            className="field-input"
            aria-label="Enrich comment"
            rows={3}
            value={comment}
            onChange={(event) => setComment(event.target.value)}
          />
        </label>
        {enrich.isError && (
          <div className="wi-inline-error" role="alert">
            <span>Enrich failed: {enrich.error.message}</span>
          </div>
        )}
        <div className="wi-modal-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={onClose}
            disabled={enrich.isPending}
          >
            Cancel
          </button>
          <button type="submit" className="btn btn-primary btn-sm" disabled={!canSubmit}>
            {enrich.isPending ? 'Enriching…' : 'Enrich item'}
          </button>
        </div>
    </Dialog>
  )
}

export function WorkItemDetailPage() {
  const params = useParams<{ provider: string; itemId: string }>()
  const provider = params.provider ?? 'tracker'
  const key = params.itemId ? decodeURIComponent(params.itemId) : undefined
  const [searchParams] = useSearchParams()
  const project = searchParams.get('project') || undefined

  const itemQuery = useWorkItem(key, project)
  const consumer = useConsumer()
  const canMutate = consumer ? roleAtLeast(consumer.role, 'operator') : false
  const [enriching, setEnriching] = useState(false)

  if (itemQuery.isPending) {
    return (
      <section className="wi-page animate-enter">
        <div
          className="wi-skeleton"
          role="status"
          aria-busy="true"
          aria-label="Loading work item"
        >
          <div className="glass-panel wi-skeleton-row" />
          <div className="glass-panel wi-skeleton-row" />
        </div>
      </section>
    )
  }

  if (itemQuery.isError || !itemQuery.data) {
    const notFound = isApiError(itemQuery.error) && itemQuery.error.status === 404
    return (
      <section className="wi-page animate-enter">
        {notFound ? (
          <div className="dash-empty">
            <h2>Item not found</h2>
            <p className="dash-empty-hint">
              {key ?? 'This item'} does not exist in the connected tracker — it may have been
              deleted or the key mistyped.
            </p>
            <Link className="btn btn-secondary" to="/work-items">
              Back to the console
            </Link>
          </div>
        ) : (
          <ProblemCard
            title="Work item unavailable"
            message={itemQuery.error?.message ?? 'The work item could not be loaded.'}
            onRetry={() => void itemQuery.refetch()}
          />
        )}
      </section>
    )
  }

  const item = itemQuery.data
  const paragraphs = descriptionParagraphs(item.description)

  return (
    <section className="wi-page animate-enter">
      <header className="wi-detail-header glass-panel">
        <div className="wi-detail-heading">
          <nav className="wi-breadcrumb" aria-label="Breadcrumb">
            <Link to="/work-items">Work items</Link>
            <span aria-hidden="true">/</span>
            <span>{provider}</span>
            <span aria-hidden="true">/</span>
            <span className="strong">{item.key}</span>
          </nav>
          <div className="wi-detail-title-row">
            <h2 className="wi-detail-title">{item.title}</h2>
            <KindChip kind={item.kind} />
            <StatusBadge status={item.status} />
            {item.url && <ExternalLink url={item.url} itemKey={item.key} />}
          </div>
        </div>
        {canMutate && (
          <div className="wi-detail-actions">
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => setEnriching(true)}
            >
              Enrich
            </button>
          </div>
        )}
      </header>

      <div className="wi-detail-card glass-panel">
        <span className="wi-field-label">Description</span>
        {paragraphs.length === 0 ? (
          <p className="wi-caption">No description.</p>
        ) : (
          <div className="wi-description">
            {paragraphs.map((paragraph, index) => (
              <p key={index}>{paragraph}</p>
            ))}
          </div>
        )}
      </div>

      {enriching && (
        <EnrichModal
          item={item}
          {...(project ? { project } : {})}
          onClose={() => setEnriching(false)}
        />
      )}
    </section>
  )
}
