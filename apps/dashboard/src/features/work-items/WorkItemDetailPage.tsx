/**
 * /work-items/:provider/:itemId — one tracker item (plan Part 2 route table).
 * The :provider segment is display context only; the fetch keys on the
 * decoded :itemId (= item key). Enrich (operator+) POSTs fields JSON + an
 * optional comment and swaps in the refreshed item the server returns.
 * A 404 renders the dash-empty 'Item not found' state, not a problem card.
 */
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'

import {
  createWorkItemMutationKey,
  workItemEnrichMutationKey,
  useEnrichWorkItem,
  useWorkItem,
  type ResolvedWorkItem,
} from '@/api/hooks/useWorkTracking'
import { usePendingMutationCount } from '@/api/hooks/usePendingMutationCount'
import { isApiError } from '@/api/errors'
import { useConsumer } from '@/auth/AuthProvider'
import { canMutateAudience } from '@/auth/RequireRole'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { Dialog } from '@/components/Dialog'
import { ProblemCard } from '@/components/ProblemCard'

import { ExternalLink, KindChip, StatusBadge } from './workItemsBits'
import {
  bindDurableMutationDraft,
  clearDurableMutationDraft,
  initializeDurableMutationDraft,
  SafeRetryStorageError,
  scopedMutationStorageKey,
  stableMutationFingerprint,
  type DurableMutationDraft,
  updateDurableMutationDraft,
} from './durableMutationDraft'
import { descriptionParagraphs, parseJsonObject, workItemPath } from './workItemsLogic'
import './work-items.css'

interface EnrichDraftState {
  fieldsText: string
  comment: string
}

function isEnrichDraftState(value: unknown): value is EnrichDraftState {
  if (!value || typeof value !== 'object') return false
  const draft = value as Partial<EnrichDraftState>
  return typeof draft.fieldsText === 'string' && typeof draft.comment === 'string'
}

function enrichFingerprint(draft: EnrichDraftState): string {
  const fields = parseJsonObject(draft.fieldsText)
  return stableMutationFingerprint(
    fields.ok
      ? { fields: fields.value, comment: draft.comment.trim() || null }
      : { invalidFieldsText: draft.fieldsText, comment: draft.comment },
  )
}

/** Fields JSON editor + comment textarea -> POST items/{key}/enrich (operator+). */
function EnrichModal({
  item,
  project,
  routeIdentity,
  onClose,
}: {
  item: ResolvedWorkItem
  project?: string
  routeIdentity: string
  onClose: (routeIdentity: string) => void
}) {
  const enrich = useEnrichWorkItem(item.connection_id, item.key)
  const writeCount = usePendingMutationCount(
    workItemEnrichMutationKey(item.connection_id, item.key),
  )
  const writePending = writeCount > 0
  const mountedRef = useRef(true)
  const storageKey = scopedMutationStorageKey(
    'apex.work-items.enrich.v2',
    project,
    item.connection_id,
    item.key,
  )
  const [attempt, setAttempt] = useState(() =>
    initializeDurableMutationDraft(
      storageKey,
      { fieldsText: '{}', comment: '' },
      isEnrichDraftState,
      enrichFingerprint,
      () => createWorkItemMutationKey('enrich'),
    ),
  )
  const [safeRetryError, setSafeRetryError] = useState<string | null>(null)
  const { fieldsText, comment } = attempt.draft

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  const fieldsParse = parseJsonObject(fieldsText)
  const hasPayload =
    fieldsParse.ok && (Object.keys(fieldsParse.value).length > 0 || comment.trim() !== '')
  const canSubmit =
    fieldsParse.ok && hasPayload && !writePending && safeRetryError === null

  function updateDraft(changes: Partial<EnrichDraftState>) {
    if (writePending) return
    setSafeRetryError(null)
    setAttempt((previous) =>
      updateDurableMutationDraft(
        storageKey,
        previous,
        { ...previous.draft, ...changes },
        enrichFingerprint,
        () => createWorkItemMutationKey('enrich'),
      ),
    )
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSubmit || !fieldsParse.ok) return
    let submittedAttempt: DurableMutationDraft<EnrichDraftState>
    try {
      submittedAttempt = bindDurableMutationDraft(storageKey, attempt, enrichFingerprint)
    } catch (error) {
      if (error instanceof SafeRetryStorageError) {
        setSafeRetryError(error.message)
        return
      }
      throw error
    }
    setAttempt(submittedAttempt)
    try {
      await enrich.mutateAsync({
        key: item.key,
        body: { fields: fieldsParse.value, comment: comment.trim() || null },
        connectionId: item.connection_id,
        idempotencyKey: submittedAttempt.idempotencyKey,
        ...(project ? { project } : {}),
      })
      clearDurableMutationDraft(storageKey, submittedAttempt.idempotencyKey)
      if (mountedRef.current) onClose(routeIdentity)
    } catch {
      // The mutation exposes its error state inline while this modal is mounted.
    }
  }

  return (
    <Dialog
      overlayClassName="wi-overlay"
      className="wi-modal glass-panel"
      ariaLabel={`Enrich ${item.key}`}
      onClose={() => onClose(routeIdentity)}
      closeOnBackdrop={!writePending}
      closeOnEscape={!writePending}
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
          disabled={writePending}
          onChange={(event) => updateDraft({ fieldsText: event.target.value })}
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
          disabled={writePending}
          onChange={(event) => updateDraft({ comment: event.target.value })}
        />
      </label>
      {enrich.isError && (
        <div className="wi-inline-error" role="alert">
          <span>Enrich failed: {enrich.error.message}</span>
        </div>
      )}
      {safeRetryError && (
        <div className="wi-inline-error" role="alert">
          <span>Enrich blocked: {safeRetryError}</span>
        </div>
      )}
      <div className="wi-modal-actions">
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => onClose(routeIdentity)}
          disabled={writePending}
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
  const navigate = useNavigate()
  const params = useParams<{ provider: string; itemId: string }>()
  const provider = params.provider ?? 'tracker'
  const key = params.itemId
  const [searchParams] = useSearchParams()
  const project = searchParams.get('project') || undefined
  const connectionId = searchParams.get('connection_id') || undefined

  const itemQuery = useWorkItem(
    key,
    project,
    connectionId,
    provider === 'tracker' ? undefined : provider,
  )
  const enrichConnectionId = itemQuery.data?.connection_id ?? connectionId ?? ''
  const enrichKey = itemQuery.data?.key ?? key ?? ''
  const enrichWriteCount = usePendingMutationCount(
    workItemEnrichMutationKey(enrichConnectionId, enrichKey),
  )
  const consumer = useConsumer()
  const consumerProjects = Array.from(
    new Set((consumer?.scopes ?? []).map((scope) => scope.project_id)),
  )
  const effectiveProject = project ?? (consumerProjects.length === 1 ? consumerProjects[0] : null)
  const canMutate = canMutateAudience(consumer, effectiveProject, null)
  const [enriching, setEnriching] = useState(false)
  const routeIdentity = JSON.stringify([
    provider,
    project ?? '',
    connectionId ?? '',
    key ?? '',
  ])
  const activeRouteIdentityRef = useRef(routeIdentity)
  activeRouteIdentityRef.current = routeIdentity

  useEffect(() => {
    setEnriching(false)
  }, [routeIdentity])

  useEffect(() => {
    const item = itemQuery.data
    if (!item || connectionId || !key) return
    void navigate(
      workItemPath(item.provider, key, project, item.connection_id),
      { replace: true },
    )
  }, [connectionId, itemQuery.data, key, navigate, project])

  if (itemQuery.isPending || (itemQuery.data && !connectionId)) {
    return (
      <section className="wi-page animate-enter">
        <div className="wi-skeleton" role="status" aria-busy="true" aria-label="Loading work item">
          <div className="glass-panel wi-skeleton-row" />
          <div className="glass-panel wi-skeleton-row" />
        </div>
      </section>
    )
  }

  if (!itemQuery.data) {
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
      {itemQuery.isError && (
        <CachedDataWarning error={itemQuery.error} onRetry={() => void itemQuery.refetch()} />
      )}
      <header className="wi-detail-header glass-panel">
        <div className="wi-detail-heading">
          <nav className="wi-breadcrumb" aria-label="Breadcrumb">
            <Link to="/work-items">Work items</Link>
            <span aria-hidden="true">/</span>
            <span>{item.provider}</span>
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
              disabled={enrichWriteCount > 0}
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
          key={routeIdentity}
          item={item}
          routeIdentity={routeIdentity}
          {...(project ? { project } : {})}
          onClose={(completedIdentity) => {
            if (activeRouteIdentityRef.current === completedIdentity) setEnriching(false)
          }}
        />
      )}
    </section>
  )
}
