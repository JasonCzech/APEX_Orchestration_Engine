/**
 * /admin/consumers — API consumer management (plan Part 2 route table + UX
 * 2.f, D7). Admin-gated.
 *
 * Create and rotate answer with the raw api_key EXACTLY ONCE; the key-reveal
 * modal is the only place it ever appears, and it cannot be dismissed until
 * the admin confirms the key is stored. Self-delete 409s render inline as
 * "You cannot delete your own consumer" (never a toast).
 */
import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router'

import {
  CONSUMER_TYPES,
  useConsumersIndex,
  useCreateConsumer,
  useDeleteConsumer,
  useRotateConsumerKey,
  useUpdateConsumer,
  type Consumer,
  type ConsumerCreated,
  type ConsumerType,
  type ScopeRef,
} from '@/api/hooks/useConsumers'
import type { Role } from '@/api/apexClient'
import { isApiError } from '@/api/errors'
import { ProblemCard } from '@/components/ProblemCard'
import { OverflowMenu } from '@/features/runs/PreflightModal'
import { formatRelative } from '@/utils/time'

import { scopesSummary } from './adminLogic'
import { AdminGate } from './adminShared'
import './admin.css'

const ROLES: readonly Role[] = ['viewer', 'operator', 'admin']
const SKELETON_ROWS = 4
const EM_DASH = '—'

const ROLE_BADGE: Record<Role, string> = {
  viewer: 'neutral',
  operator: 'info',
  admin: 'accent',
}

interface ScopeDraft {
  project_id: string
  app_id: string
}

function scopesToPayload(drafts: ScopeDraft[]): ScopeRef[] {
  return drafts
    .filter((draft) => draft.project_id.trim() !== '')
    .map((draft) => ({
      project_id: draft.project_id.trim(),
      app_id: draft.app_id.trim() || null,
    }))
}

/** Scope rows: project_id + optional app_id, add/remove. */
function ScopesEditor({
  scopes,
  onChange,
}: {
  scopes: ScopeDraft[]
  onChange: (next: ScopeDraft[]) => void
}) {
  return (
    <div className="adm-scopes-editor">
      {scopes.map((scope, index) => (
        <div key={index} className="adm-scopes-row">
          <input
            type="text"
            className="field-input"
            aria-label={`Scope ${index + 1} project`}
            placeholder="project_id"
            value={scope.project_id}
            onChange={(event) =>
              onChange(
                scopes.map((s, i) => (i === index ? { ...s, project_id: event.target.value } : s)),
              )
            }
          />
          <input
            type="text"
            className="field-input"
            aria-label={`Scope ${index + 1} app (optional)`}
            placeholder="app_id (optional)"
            value={scope.app_id}
            onChange={(event) =>
              onChange(
                scopes.map((s, i) => (i === index ? { ...s, app_id: event.target.value } : s)),
              )
            }
          />
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            aria-label={`Remove scope ${index + 1}`}
            onClick={() => onChange(scopes.filter((_, i) => i !== index))}
          >
            Remove
          </button>
        </div>
      ))}
      <button
        type="button"
        className="btn btn-secondary btn-sm adm-scopes-add"
        onClick={() => onChange([...scopes, { project_id: '', app_id: '' }])}
      >
        Add scope
      </button>
    </div>
  )
}

/**
 * The one-time key reveal. No overlay click, no Escape — the only way out is
 * the [I've stored it] button, gated behind an explicit confirmation check.
 */
function KeyRevealModal({
  title,
  created,
  onClose,
}: {
  title: string
  created: ConsumerCreated
  onClose: () => void
}) {
  const [stored, setStored] = useState(false)
  const [copied, setCopied] = useState(false)

  function copy() {
    void navigator.clipboard?.writeText(created.api_key).then(
      () => setCopied(true),
      () => setCopied(false),
    )
  }

  return (
    <div className="adm-overlay">
      <div className="adm-modal glass-panel" role="dialog" aria-modal="true" aria-label={title}>
        <h2 className="adm-panel-title">{title}</h2>
        <p className="adm-modal-caption">
          API key for <strong>{created.name}</strong>. Store it now — it will never be shown
          again.
        </p>
        <div className="adm-key-row">
          <code className="adm-key" data-testid="revealed-api-key">
            {created.api_key}
          </code>
          <button type="button" className="btn btn-secondary btn-sm" onClick={copy}>
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
        <label className="adm-confirm-check">
          <input
            type="checkbox"
            checked={stored}
            onChange={(event) => setStored(event.target.checked)}
            aria-label="I have stored this key somewhere safe"
          />
          <span>I have stored this key somewhere safe</span>
        </label>
        <div className="adm-panel-actions">
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={!stored}
            onClick={onClose}
          >
            I&rsquo;ve stored it
          </button>
        </div>
      </div>
    </div>
  )
}

/** Inline create panel: name / type / role segmented / scopes editor. */
function CreateConsumerPanel({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: (created: ConsumerCreated) => void
}) {
  const create = useCreateConsumer()
  const [name, setName] = useState('')
  const [consumerType, setConsumerType] = useState<ConsumerType>('headless')
  const [role, setRole] = useState<Role>('viewer')
  const [scopes, setScopes] = useState<ScopeDraft[]>([{ project_id: '', app_id: '' }])

  const canSubmit = name.trim() !== '' && !create.isPending

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSubmit) return
    create.mutate(
      {
        name: name.trim(),
        consumer_type: consumerType,
        role,
        scopes: scopesToPayload(scopes),
      },
      {
        onSuccess: (created) => {
          onClose()
          onCreated(created)
        },
      },
    )
  }

  return (
    <form className="glass-panel adm-panel" onSubmit={submit} aria-label="New consumer">
      <h2 className="adm-panel-title">New consumer</h2>
      <div className="adm-form-grid">
        <label className="adm-field">
          <span className="adm-field-label">Name</span>
          <input
            type="text"
            className="field-input"
            aria-label="Name"
            placeholder="ci-bot"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label className="adm-field">
          <span className="adm-field-label">Type</span>
          <select
            className="field-select"
            aria-label="Type"
            value={consumerType}
            onChange={(event) => setConsumerType(event.target.value as ConsumerType)}
          >
            {CONSUMER_TYPES.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
        <div className="adm-field">
          <span className="adm-field-label">Role</span>
          <div className="adm-segmented" role="group" aria-label="Role">
            {ROLES.map((option) => (
              <button
                key={option}
                type="button"
                className={`adm-segment${role === option ? ' active' : ''}`}
                aria-pressed={role === option}
                onClick={() => setRole(option)}
              >
                {option}
              </button>
            ))}
          </div>
        </div>
      </div>
      <div className="adm-field">
        <span className="adm-field-label">Scopes</span>
        <ScopesEditor scopes={scopes} onChange={setScopes} />
      </div>
      {create.isError && (
        <div className="adm-inline-error" role="alert">
          <span>{create.error.message}</span>
        </div>
      )}
      <div className="adm-panel-actions">
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={onClose}
          disabled={create.isPending}
        >
          Cancel
        </button>
        <button type="submit" className="btn btn-primary btn-sm" disabled={!canSubmit}>
          {create.isPending ? 'Creating…' : 'Create consumer'}
        </button>
      </div>
    </form>
  )
}

/** Edit modal: role / scopes / enabled PATCH. */
function EditConsumerModal({ consumer, onClose }: { consumer: Consumer; onClose: () => void }) {
  const update = useUpdateConsumer()
  const [role, setRole] = useState<Role>(consumer.role)
  const [enabled, setEnabled] = useState(consumer.enabled)
  const [scopes, setScopes] = useState<ScopeDraft[]>(() =>
    consumer.scopes.map((scope) => ({ project_id: scope.project_id, app_id: scope.app_id ?? '' })),
  )

  function close() {
    if (update.isPending) return
    onClose()
  }

  return (
    <div
      className="adm-overlay"
      onClick={(event) => {
        if (event.target === event.currentTarget) close()
      }}
      onKeyDown={(event) => {
        if (event.key === 'Escape') close()
      }}
    >
      <div
        className="adm-modal glass-panel"
        role="dialog"
        aria-modal="true"
        aria-label={`Edit consumer ${consumer.name}`}
      >
        <h2 className="adm-panel-title">Edit {consumer.name}</h2>
        <div className="adm-field">
          <span className="adm-field-label">Role</span>
          <div className="adm-segmented" role="group" aria-label="Role">
            {ROLES.map((option) => (
              <button
                key={option}
                type="button"
                className={`adm-segment${role === option ? ' active' : ''}`}
                aria-pressed={role === option}
                onClick={() => setRole(option)}
              >
                {option}
              </button>
            ))}
          </div>
        </div>
        <div className="adm-field">
          <span className="adm-field-label">Scopes</span>
          <ScopesEditor scopes={scopes} onChange={setScopes} />
        </div>
        <label className="adm-confirm-check">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(event) => setEnabled(event.target.checked)}
            aria-label="Enabled"
          />
          <span>Enabled</span>
        </label>
        {update.isError && (
          <div className="adm-inline-error" role="alert">
            <span>{update.error.message}</span>
          </div>
        )}
        <div className="adm-panel-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={close}
            disabled={update.isPending}
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={update.isPending}
            onClick={() =>
              update.mutate(
                {
                  consumerId: consumer.id,
                  body: { role, enabled, scopes: scopesToPayload(scopes) },
                },
                { onSuccess: onClose },
              )
            }
          >
            {update.isPending ? 'Saving…' : 'Save changes'}
          </button>
        </div>
      </div>
    </div>
  )
}

/** Rotate confirm — success hands the one-time payload up for the reveal modal. */
function RotateConsumerModal({
  consumer,
  onClose,
  onRotated,
}: {
  consumer: Consumer
  onClose: () => void
  onRotated: (rotated: ConsumerCreated) => void
}) {
  const rotate = useRotateConsumerKey()

  function close() {
    if (rotate.isPending) return
    onClose()
  }

  return (
    <div
      className="adm-overlay"
      onClick={(event) => {
        if (event.target === event.currentTarget) close()
      }}
      onKeyDown={(event) => {
        if (event.key === 'Escape') close()
      }}
    >
      <div
        className="adm-modal glass-panel"
        role="dialog"
        aria-modal="true"
        aria-label={`Rotate key for ${consumer.name}`}
      >
        <h2 className="adm-panel-title">Rotate API key</h2>
        <p className="adm-modal-caption">
          Rotating issues a new key for <strong>{consumer.name}</strong> and revokes the current
          one immediately. Anything still using the old key will start failing.
        </p>
        {rotate.isError && (
          <div className="adm-inline-error" role="alert">
            <span>{rotate.error.message}</span>
          </div>
        )}
        <div className="adm-panel-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={close}
            disabled={rotate.isPending}
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={rotate.isPending}
            onClick={() =>
              rotate.mutate(consumer.id, {
                onSuccess: (rotated) => {
                  onClose()
                  onRotated(rotated)
                },
              })
            }
          >
            {rotate.isPending ? 'Rotating…' : 'Rotate key'}
          </button>
        </div>
      </div>
    </div>
  )
}

/** Delete confirm — a 409 means self-delete and renders the friendly line inline. */
function DeleteConsumerModal({ consumer, onClose }: { consumer: Consumer; onClose: () => void }) {
  const remove = useDeleteConsumer()
  const selfDelete = remove.isError && isApiError(remove.error) && remove.error.status === 409

  function close() {
    if (remove.isPending) return
    onClose()
  }

  return (
    <div
      className="adm-overlay"
      onClick={(event) => {
        if (event.target === event.currentTarget) close()
      }}
      onKeyDown={(event) => {
        if (event.key === 'Escape') close()
      }}
    >
      <div
        className="adm-modal glass-panel"
        role="dialog"
        aria-modal="true"
        aria-label={`Delete consumer ${consumer.name}`}
      >
        <h2 className="adm-panel-title">Delete consumer</h2>
        <p className="adm-modal-caption">
          This permanently removes <strong>{consumer.name}</strong> and revokes its key.
        </p>
        {remove.isError && (
          <div className="adm-inline-error" role="alert">
            <span>
              {selfDelete ? 'You cannot delete your own consumer' : remove.error.message}
            </span>
          </div>
        )}
        <div className="adm-panel-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={close}
            disabled={remove.isPending}
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-danger btn-sm"
            disabled={remove.isPending || selfDelete}
            onClick={() => remove.mutate(consumer.id, { onSuccess: onClose })}
          >
            {remove.isPending ? 'Deleting…' : 'Delete consumer'}
          </button>
        </div>
      </div>
    </div>
  )
}

function ConsumerRow({
  consumer,
  onEdit,
  onRotate,
  onDelete,
}: {
  consumer: Consumer
  onEdit: (consumer: Consumer) => void
  onRotate: (consumer: Consumer) => void
  onDelete: (consumer: Consumer) => void
}) {
  const navigate = useNavigate()
  return (
    <tr data-testid={`consumer-row-${consumer.id}`}>
      <td className="strong">{consumer.name}</td>
      <td>
        <span className="dash-context-chip">{consumer.consumer_type}</span>
      </td>
      <td>
        <span className={`status-badge ${ROLE_BADGE[consumer.role]}`}>{consumer.role}</span>
      </td>
      <td>{scopesSummary(consumer.scopes)}</td>
      <td>
        {consumer.enabled ? (
          <span className="status-badge success">enabled</span>
        ) : (
          <span className="status-badge neutral">disabled</span>
        )}
      </td>
      <td className="adm-time" title={consumer.last_used_at ?? undefined}>
        {consumer.last_used_at ? formatRelative(consumer.last_used_at) : EM_DASH}
      </td>
      <td>
        <code className="adm-fingerprint">{consumer.key_fingerprint || EM_DASH}</code>
      </td>
      <td className="adm-actions-cell">
        <OverflowMenu
          label={`Consumer actions: ${consumer.name}`}
          items={[
            { label: 'Open', onSelect: () => void navigate(`/admin/consumers/${consumer.id}`) },
            { label: 'Edit', onSelect: () => onEdit(consumer) },
            { label: 'Rotate key…', onSelect: () => onRotate(consumer) },
            { label: 'Delete…', onSelect: () => onDelete(consumer) },
          ]}
        />
      </td>
    </tr>
  )
}

function ConsumersContent() {
  const consumers = useConsumersIndex()
  const [creating, setCreating] = useState(false)
  const [reveal, setReveal] = useState<{ title: string; created: ConsumerCreated } | null>(null)
  const [editing, setEditing] = useState<Consumer | null>(null)
  const [rotating, setRotating] = useState<Consumer | null>(null)
  const [deleting, setDeleting] = useState<Consumer | null>(null)

  return (
    <section className="adm-page animate-enter">
      <header className="adm-toolbar glass-panel">
        <h2 className="adm-page-title">API consumers</h2>
        {!creating && (
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={() => setCreating(true)}
          >
            New consumer
          </button>
        )}
      </header>

      {creating && (
        <CreateConsumerPanel
          onClose={() => setCreating(false)}
          onCreated={(created) => setReveal({ title: 'API key created', created })}
        />
      )}

      {consumers.isPending ? (
        <div className="adm-skeleton" role="status" aria-busy="true" aria-label="Loading consumers">
          {Array.from({ length: SKELETON_ROWS }, (_, i) => (
            <div key={i} className="glass-panel adm-skeleton-row" />
          ))}
        </div>
      ) : consumers.isError ? (
        <ProblemCard
          title="Consumers unavailable"
          message={consumers.error.message}
          onRetry={() => void consumers.refetch()}
        />
      ) : consumers.data.length === 0 ? (
        <div className="dash-empty">
          <h2>No consumers yet</h2>
          <p className="dash-empty-hint">
            Consumers are the API identities (dashboards, CI bots, internal services) that hold
            keys.
          </p>
        </div>
      ) : (
        <div className="data-table-wrap">
          <table className="data-table striped adm-consumers-table" aria-label="Consumers">
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th>Role</th>
                <th>Scopes</th>
                <th>Enabled</th>
                <th>Last used</th>
                <th>Key</th>
                <th className="adm-actions-cell">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {consumers.data.map((consumer) => (
                <ConsumerRow
                  key={consumer.id}
                  consumer={consumer}
                  onEdit={setEditing}
                  onRotate={setRotating}
                  onDelete={setDeleting}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && <EditConsumerModal consumer={editing} onClose={() => setEditing(null)} />}
      {rotating && (
        <RotateConsumerModal
          consumer={rotating}
          onClose={() => setRotating(null)}
          onRotated={(rotated) => setReveal({ title: 'API key rotated', created: rotated })}
        />
      )}
      {deleting && <DeleteConsumerModal consumer={deleting} onClose={() => setDeleting(null)} />}
      {reveal && (
        <KeyRevealModal
          title={reveal.title}
          created={reveal.created}
          onClose={() => setReveal(null)}
        />
      )}
    </section>
  )
}

export function ConsumersPage() {
  return (
    <AdminGate>
      <ConsumersContent />
    </AdminGate>
  )
}
