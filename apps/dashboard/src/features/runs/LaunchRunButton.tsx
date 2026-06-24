import { useId, useState } from 'react'
import { useNavigate } from 'react-router'

import { useLaunchRun } from '@/api/hooks/useLaunchRun'
import { Dialog } from '@/components/Dialog'

import './live.css'

/**
 * Minimal D2 launch entry point: btn-primary + a small modal (title, request,
 * project). Gates run ALL-AUTO in D2 (see launchRun.ts); the gate-policy
 * matrix, phase subsets, and drafts arrive with the D4 wizard at /runs/new.
 * On success navigates straight into the live run at ?tab=activity.
 */
export function LaunchRunButton() {
  const [open, setOpen] = useState(false)
  const [title, setTitle] = useState('')
  const [request, setRequest] = useState('')
  const [project, setProject] = useState('demo')
  const navigate = useNavigate()
  const launch = useLaunchRun()
  const titleId = useId()

  const canSubmit = title.trim().length > 0 && request.trim().length > 0 && !launch.isPending

  function close() {
    if (launch.isPending) return
    setOpen(false)
    launch.reset()
  }

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!canSubmit) return
    launch.mutate(
      {
        title: title.trim(),
        request: request.trim(),
        projectId: project.trim() || 'demo',
      },
      {
        onSuccess: ({ threadId }) => {
          setOpen(false)
          void navigate(`/runs/${threadId}?tab=activity`)
        },
      },
    )
  }

  return (
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
                onChange={(event) => setTitle(event.target.value)}
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
                onChange={(event) => setRequest(event.target.value)}
                rows={4}
                placeholder="What should this pipeline run test?"
              />
            </label>
            <label className="launch-field">
              <span className="launch-field-label">Project</span>
              <input
                type="text"
                className="field-input"
                value={project}
                onChange={(event) => setProject(event.target.value)}
              />
            </label>
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
  )
}
