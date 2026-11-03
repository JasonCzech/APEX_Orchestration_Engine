import { useTimestampPreference } from '@/theme/preferences'
import { Skeleton } from '@/components/ux/Skeleton'
/**
 * /admin/consumers/:id — detail view for one consumer (plan Part 2 route table,
 * D7). Admin-gated. Supports inline Edit, Rotate key, and Delete modals as well
 * as deep-linkable identity inspection.
 */
import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'

import type { Role } from '@/api/apexClient'
import {
  consumerWriteMutationKey,
  useConsumerDetail,
  useCurrentPrincipal,
} from '@/api/hooks/useConsumers'
import { usePendingMutationCount } from '@/api/hooks/usePendingMutationCount'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { ProblemCard } from '@/components/ProblemCard'
import { CopyableId } from '@/components/ux/CopyableId'
import { formatRelative } from '@/utils/time'

import { scopeLabel } from './adminLogic'
import { AdminGate } from './adminShared'
import {
  DeleteConsumerModal,
  EditConsumerModal,
  RotateConsumerModal,
} from './ConsumersPage'
import './admin.css'

const EM_DASH = '—'

const ROLE_BADGE: Record<Role, string> = {
  viewer: 'neutral',
  operator: 'info',
  admin: 'accent',
}

function ConsumerDetailContent() {
  const { id = '' } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const consumer = useConsumerDetail(id)
  const principal = useCurrentPrincipal()
  const writeCount = usePendingMutationCount(consumerWriteMutationKey(id))
  const writePending = writeCount > 0
  const [editing, setEditing] = useState(false)
  const [rotating, setRotating] = useState(false)
  const [deleting, setDeleting] = useState(false)

  if (consumer.isPending) {
    return (
      <Skeleton label="Loading consumer" rows={1} height={120} />
    )
  }

  if (!consumer.data) {
    return (
      <ProblemCard
        title="Consumer unavailable"
        message={consumer.error?.message ?? 'The consumer could not be loaded.'}
        onRetry={() => void consumer.refetch()}
      />
    )
  }

  const data = consumer.data
  const isCurrentConsumer = principal.data?.principal_id === data.id
  const actionsReady = principal.isSuccess && !writePending

  return (
    <section className="adm-page animate-enter">
      {consumer.isError && (
        <CachedDataWarning error={consumer.error} onRetry={() => void consumer.refetch()} />
      )}
      <header className="adm-detail-header glass-panel">
        <div className="adm-detail-heading">
          <nav className="adm-breadcrumb" aria-label="Breadcrumb">
            <Link to="/admin/consumers">API consumers</Link>
          </nav>
          <div className="adm-detail-title-row">
            <h2 className="adm-detail-title">{data.name}</h2>
            <span className="dash-context-chip">{data.consumer_type}</span>
            <span className={`status-badge ${ROLE_BADGE[data.role]}`}>{data.role}</span>
            {data.enabled ? (
              <span className="status-badge success">enabled</span>
            ) : (
              <span className="status-badge neutral">disabled</span>
            )}
          </div>
        </div>
        <div className="adm-detail-actions">
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={!actionsReady}
            onClick={() => setEditing(true)}
          >
            Edit access
          </button>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={!actionsReady}
            onClick={() => setRotating(true)}
          >
            Rotate key…
          </button>
          <button
            type="button"
            className="btn btn-danger btn-sm"
            disabled={!actionsReady}
            onClick={() => setDeleting(true)}
          >
            Delete…
          </button>
        </div>
      </header>

      <div className="adm-card-panel glass-panel">
        <dl className="adm-info-grid">
          <dt>Consumer ID</dt>
          <dd>
            <CopyableId value={data.id} label="Consumer ID" />
          </dd>
          <dt>Key fingerprint</dt>
          <dd>
            <code className="adm-fingerprint">{data.key_fingerprint || EM_DASH}</code>
          </dd>
          <dt>Created</dt>
          <dd title={data.created_at ?? undefined}>
            {data.created_at ? formatRelative(data.created_at) : EM_DASH}
          </dd>
          <dt>Last used</dt>
          <dd title={data.last_used_at ?? undefined}>
            {data.last_used_at ? formatRelative(data.last_used_at) : EM_DASH}
          </dd>
          <dt>Scopes</dt>
          <dd>
            {data.scopes.length === 0 ? (
              <span className="adm-muted">{EM_DASH}</span>
            ) : (
              <ul className="adm-scope-list">
                {data.scopes.map((scope) => (
                  <li key={scopeLabel(scope)}>
                    <code className="adm-fingerprint">{scopeLabel(scope)}</code>
                  </li>
                ))}
              </ul>
            )}
          </dd>
        </dl>
      </div>

      {editing && (
        <EditConsumerModal
          consumer={data}
          isCurrentConsumer={isCurrentConsumer}
          onClose={() => setEditing(false)}
        />
      )}
      {rotating && (
        <RotateConsumerModal
          consumer={data}
          isCurrentConsumer={isCurrentConsumer}
          onClose={() => setRotating(false)}
        />
      )}
      {deleting && (
        <DeleteConsumerModal
          consumer={data}
          onClose={() => setDeleting(false)}
          onDeleted={() => navigate('/admin/consumers')}
        />
      )}
    </section>
  )
}

export function ConsumerDetailPage() {
  useTimestampPreference()
  return (
    <AdminGate>
      <ConsumerDetailContent />
    </AdminGate>
  )
}
