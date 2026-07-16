import { useId, useState } from 'react'
import { useNavigate } from 'react-router'

import { useLaunchRun } from '@/api/hooks/useLaunchRun'
import { useOptionalConsumer } from '@/auth/AuthProvider'
import { isGlobalAdmin, RequireRole } from '@/auth/RequireRole'
import { Dialog } from '@/components/Dialog'

import './live.css'

interface ScopedAudience {
  value: string
  projectId: string
  appId?: string
  label: string
}

function scopedAudiences(
  scopes: ReadonlyArray<{ project_id: string; app_id?: string | null }>,
): ScopedAudience[] {
  const seen = new Set<string>()
  const audiences: ScopedAudience[] = []
  for (const scope of scopes) {
    const projectId = scope.project_id.trim()
    const appId = scope.app_id == null ? undefined : scope.app_id.trim()
    // Never upgrade malformed app-scoped metadata into project-wide access.
    if (projectId.length === 0 || (scope.app_id != null && (appId?.length ?? 0) === 0)) continue
    const value = JSON.stringify([projectId, appId ?? null])
    if (seen.has(value)) continue
    seen.add(value)
    audiences.push({
      value,
      projectId,
      ...(appId !== undefined ? { appId } : {}),
      label: appId ? `${projectId} / ${appId}` : `${projectId} (project-wide)`,
    })
  }
  return audiences.sort((left, right) => left.label.localeCompare(right.label))
}

/**
 * Minimal D2 launch entry point: btn-primary + a small modal (title, request,
 * project). Gates run ALL-AUTO in D2 (see launchRun.ts); the gate-policy
 * matrix, phase subsets, and drafts arrive with the D4 wizard at /runs/new.
 * On success navigates straight into the live run's Pipeline Log tab.
 */
export function LaunchRunButton() {
  const consumer = useOptionalConsumer()
  const audiences = scopedAudiences(consumer?.scopes ?? [])
  const isScoped = audiences.length > 0
  const [open, setOpen] = useState(false)
  const [title, setTitle] = useState('')
  const [request, setRequest] = useState('')
  const [project, setProject] = useState('demo')
  const [application, setApplication] = useState('')
  const [audienceValue, setAudienceValue] = useState(() => audiences[0]?.value ?? '')
  const navigate = useNavigate()
  const launch = useLaunchRun()
  const titleId = useId()

  const selectedAudience = isScoped
    ? audiences.find((audience) => audience.value === audienceValue) ?? audiences[0]
    : undefined
  const projectId = selectedAudience?.projectId ?? project.trim()
  const appId = selectedAudience?.appId ?? (application.trim() || undefined)
  const lacksSafeAudience =
    consumer !== undefined &&
    consumer !== null &&
    audiences.length === 0 &&
    !isGlobalAdmin(consumer)
  const canSubmit =
    title.trim().length > 0 &&
    request.trim().length > 0 &&
    projectId.length > 0 &&
    (!isScoped || selectedAudience !== undefined) &&
    !launch.isPending

  function resetAttempt() {
    setTitle('')
    setRequest('')
    setProject('demo')
    setApplication('')
    setAudienceValue(audiences[0]?.value ?? '')
    launch.reset()
  }

  function prepareEditAfterFailure() {
    if (!launch.isError) return
    launch.reset()
  }

  function close() {
    if (launch.isPending) return
    setOpen(false)
    resetAttempt()
  }

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!canSubmit) return
    launch.mutate(
      {
        title: title.trim(),
        request: request.trim(),
        projectId,
        ...(appId ? { appId } : {}),
      },
      {
        onSuccess: ({ threadId }) => {
          setOpen(false)
          resetAttempt()
          void navigate(`/runs/${threadId}?tab=log`)
        },
      },
    )
  }

  if (lacksSafeAudience) return null

  return (
    <RequireRole role="operator">
      <>
      <button type="button" className="btn btn-primary btn-sm" onClick={() => setOpen(true)}>
        New run
      </button>
      {open && (
        <Dialog
          overlayClassName="launch-modal-overlay"
          className="launch-modal glass-panel"
          onClose={close}
          labelledBy={titleId}
        >
          <h2 className="launch-modal-title" id={titleId}>
            Launch pipeline run
          </h2>
          <p className="launch-modal-caption">
            D2 minimal launch — all gates run auto (no approvals). Use the D4 wizard for phase
            subsets and gate policies once it lands.
          </p>
          <form onSubmit={submit}>
            <label className="launch-field">
              <span className="launch-field-label">Title</span>
              <input
                type="text"
                className="field-input"
                value={title}
                disabled={launch.isPending}
                onChange={(event) => {
                  prepareEditAfterFailure()
                  setTitle(event.target.value)
                }}
                placeholder="Checkout latency regression"
                /* First field of a just-opened modal — focus is expected here. */
                autoFocus
              />
            </label>
            <label className="launch-field">
              <span className="launch-field-label">Request</span>
              <textarea
                className="field-input launch-request"
                value={request}
                disabled={launch.isPending}
                onChange={(event) => {
                  prepareEditAfterFailure()
                  setRequest(event.target.value)
                }}
                rows={4}
                placeholder="What should this pipeline run test?"
              />
            </label>
            {isScoped ? (
              <>
                <label className="launch-field">
                  <span className="launch-field-label">Project / application scope</span>
                  <select
                    className="field-input"
                    value={selectedAudience?.value ?? ''}
                    disabled={launch.isPending}
                    onChange={(event) => {
                      prepareEditAfterFailure()
                      setAudienceValue(event.target.value)
                      setApplication('')
                    }}
                  >
                    {audiences.map((audience) => (
                      <option key={audience.value} value={audience.value}>
                        {audience.label}
                      </option>
                    ))}
                  </select>
                </label>
                {selectedAudience?.appId === undefined && (
                  <label className="launch-field">
                    <span className="launch-field-label">Application (optional)</span>
                    <input
                      type="text"
                      className="field-input"
                      value={application}
                      disabled={launch.isPending}
                      onChange={(event) => {
                        prepareEditAfterFailure()
                        setApplication(event.target.value)
                      }}
                    />
                  </label>
                )}
              </>
            ) : (
              <>
                <label className="launch-field">
                  <span className="launch-field-label">Project</span>
                  <input
                    type="text"
                    className="field-input"
                    value={project}
                    disabled={launch.isPending}
                    onChange={(event) => {
                      prepareEditAfterFailure()
                      setProject(event.target.value)
                    }}
                  />
                </label>
                <label className="launch-field">
                  <span className="launch-field-label">Application (optional)</span>
                  <input
                    type="text"
                    className="field-input"
                    value={application}
                    disabled={launch.isPending}
                    onChange={(event) => {
                      prepareEditAfterFailure()
                      setApplication(event.target.value)
                    }}
                  />
                </label>
              </>
            )}
            {launch.isError && (
              <div className="tonal-card danger" role="alert">
                Launch failed: {launch.error.message}
              </div>
            )}
            <div className="launch-modal-actions">
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={close}
                disabled={launch.isPending}
              >
                Cancel
              </button>
              <button type="submit" className="btn btn-primary btn-sm" disabled={!canSubmit}>
                {launch.isPending ? 'Launching…' : 'Launch run'}
              </button>
            </div>
          </form>
        </Dialog>
      )}
      </>
    </RequireRole>
  )
}
