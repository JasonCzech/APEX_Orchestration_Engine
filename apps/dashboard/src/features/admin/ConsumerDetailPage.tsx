/**
 * /admin/consumers/:id — read view for one consumer (plan Part 2 route table,
 * D7). Admin-gated. Mutations (edit / rotate / delete) live on the list's row
 * actions; this page is the deep-linkable identity card.
 */
import { Link, useParams } from 'react-router'

import { useConsumerDetail } from '@/api/hooks/useConsumers'
import { ProblemCard } from '@/components/ProblemCard'
import { formatRelative } from '@/utils/time'

import { scopeLabel } from './adminLogic'
import { AdminGate } from './adminShared'
import './admin.css'

const EM_DASH = '—'

function ConsumerDetailContent() {
  const { id = '' } = useParams<{ id: string }>()
  const consumer = useConsumerDetail(id)

  if (consumer.isPending) {
    return (
      <div
        className="adm-skeleton animate-enter"
        role="status"
        aria-busy="true"
        aria-label="Loading consumer"
      >
        <div className="glass-panel adm-skeleton-card" />
      </div>
    )
  }

  if (consumer.isError) {
    return (
      <ProblemCard
        title="Consumer unavailable"
        message={consumer.error.message}
        onRetry={() => void consumer.refetch()}
      />
    )
  }

  const data = consumer.data

  return (
    <section className="adm-page animate-enter">
      <header className="adm-detail-header glass-panel">
        <div className="adm-detail-heading">
          <nav className="adm-breadcrumb" aria-label="Breadcrumb">
            <Link to="/admin/consumers">Consumers</Link>
          </nav>
          <div className="adm-detail-title-row">
            <h2 className="adm-detail-title">{data.name}</h2>
            <span className="dash-context-chip">{data.consumer_type}</span>
            <span className="status-badge accent">{data.role}</span>
            {data.enabled ? (
              <span className="status-badge success">enabled</span>
            ) : (
              <span className="status-badge neutral">disabled</span>
            )}
          </div>
        </div>
      </header>

      <div className="adm-card-panel glass-panel">
        <dl className="adm-info-grid">
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
        <p className="adm-muted">
          Edit, key rotation and delete live on the consumers list&rsquo;s row actions.
        </p>
      </div>
    </section>
  )
}

export function ConsumerDetailPage() {
  return (
    <AdminGate>
      <ConsumerDetailContent />
    </AdminGate>
  )
}
