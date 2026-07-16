import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react'
import { useNavigate, useSearchParams } from 'react-router'

import { useDraftsList } from '@/api/hooks/useDrafts'
import { useOptionalAuth } from '@/auth/AuthProvider'
import { getApiKeyRevision, getSessionRevision } from '@/auth/keyStorage'
import {
  canMutateAudience,
  RequireRole,
  roleAtLeast,
} from '@/auth/RequireRole'
import { CachedDataWarning } from '@/components/CachedDataWarning'

import { ConfigStep } from './steps/ConfigStep'
import { ContextStep } from './steps/ContextStep'
import { PromptsStep } from './steps/PromptsStep'
import { ReviewStep } from './steps/ReviewStep'
import { ScopeStep } from './steps/ScopeStep'
import { WorkItemsStep } from './steps/WorkItemsStep'
import { canPersistWizardDraft, useDraft, type DraftUpdater } from './useDraft'
import { useWizardLaunch } from './useWizardLaunch'
import {
  allIssues,
  isWizardStep,
  STEP_LABELS,
  WIZARD_STEPS,
  type WizardStepId,
} from './wizardState'

import './wizard.css'

const SAVE_LABELS: Record<string, string> = {
  idle: '',
  pending: 'Unsaved changes',
  saving: 'Saving…',
  saved: 'Draft saved',
  error: 'Draft save failed',
}

/** Props every step receives: the draft plus the autosaving updater. */
export interface StepProps {
  draft: import('./wizardState').WizardDraft
  onChange: (updater: import('./useDraft').DraftUpdater) => void
  draftGeneration?: number
  isDraftGenerationCurrent?: (generation: number) => boolean
  onPendingStart?: () => () => void
  maxContextPackets?: number
  disabled?: boolean
}

type PendingWizardStep = 'work-items' | 'context'

interface PendingWizardOperation {
  step: PendingWizardStep
  draftGeneration: number
}

function WizardSection({
  id,
  title,
  description,
  active,
  children,
}: {
  id: WizardStepId
  title: string
  description: string
  active: boolean
  children: ReactNode
}) {
  return (
    <section
      id={`wizard-panel-${id}`}
      role="tabpanel"
      className="glass-panel wizard-section"
      aria-labelledby={`wizard-tab-${id}`}
      hidden={!active}
    >
      <div className="wizard-section-head">
        <div>
          <span className="wizard-section-kicker">{STEP_LABELS[id]}</span>
          <h3 className="wizard-section-title">{title}</h3>
        </div>
        <p className="wizard-section-description">{description}</p>
      </div>
      {children}
    </section>
  )
}

const WIZARD_SECTION_COPY: Record<WizardStepId, { title: string; description: string }> = {
  scope: {
    title: 'Run Scope',
    description:
      'Name the pipeline, describe the request, and choose the project, application, and target environment.',
  },
  'work-items': {
    title: 'Work Items',
    description:
      'Attach tickets or stories that should anchor the pipeline context and downstream reporting.',
  },
  context: {
    title: 'Context',
    description:
      'Add documents and context summaries so the phases have the evidence they need before execution starts.',
  },
  config: {
    title: 'Execution Configuration',
    description:
      'Choose the engine, phase coverage, and review policy. Enable full manual step-through by keeping every phase gated.',
  },
  prompts: {
    title: 'Prompt Selection',
    description:
      'Review the focused phase system prompt and the selected application requirements prompt.',
  },
  review: {
    title: 'Launch Preview',
    description:
      'Review the launch plan. Selected work items and documents are resolved into agent context when the pipeline starts.',
  },
}

function afterNextPaint(callback: () => void) {
  if (typeof window.requestAnimationFrame === 'function') {
    window.requestAnimationFrame(callback)
  } else {
    window.setTimeout(callback, 0)
  }
}

function WizardStepContent({
  step,
  draft,
  onChange,
  onEditStep,
  draftGeneration = 0,
  isDraftGenerationCurrent,
  onStepPendingStart,
  maxContextPackets,
  disabled,
}: StepProps & {
  step: WizardStepId
  onEditStep: (step: WizardStepId) => void
  onStepPendingStart: (
    step: PendingWizardStep,
    draftGeneration: number,
  ) => () => void
}) {
  switch (step) {
    case 'scope':
      return <ScopeStep draft={draft} onChange={onChange} />
    case 'work-items':
      return (
        <WorkItemsStep
          draft={draft}
          onChange={onChange}
          draftGeneration={draftGeneration}
          isDraftGenerationCurrent={isDraftGenerationCurrent}
          onPendingStart={() => onStepPendingStart('work-items', draftGeneration)}
        />
      )
    case 'context':
      return (
        <ContextStep
          draft={draft}
          onChange={onChange}
          maxContextPackets={maxContextPackets}
          disabled={disabled}
          draftGeneration={draftGeneration}
          isDraftGenerationCurrent={isDraftGenerationCurrent}
          onPendingStart={() => onStepPendingStart('context', draftGeneration)}
        />
      )
    case 'config':
      return <ConfigStep draft={draft} onChange={onChange} />
    case 'prompts':
      return <PromptsStep draft={draft} onChange={onChange} />
    case 'review':
      return <ReviewStep draft={draft} onEditStep={onEditStep} maxContextPackets={maxContextPackets} />
  }
}

export function NewRunWizardPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const urlDraftId = searchParams.get('draft')
  return (
    <NewRunWizardContent
      urlDraftId={urlDraftId}
      searchParams={searchParams}
      setSearchParams={setSearchParams}
    />
  )
}

function NewRunWizardContent({
  urlDraftId,
  searchParams,
  setSearchParams,
}: {
  urlDraftId: string | null
  searchParams: URLSearchParams
  setSearchParams: ReturnType<typeof useSearchParams>[1]
}) {
  const auth = useOptionalAuth()
  const authState = auth?.state
  const navigate = useNavigate()
  const rawStep = searchParams.get('step')
  const activeStep: WizardStepId = isWizardStep(rawStep) ? rawStep : 'scope'
  const pendingOperationsRef = useRef(new Map<number, PendingWizardOperation>())
  const nextPendingOperationIdRef = useRef(0)
  const [pendingStepCount, setPendingStepCount] = useState(0)
  const mountedRef = useRef(true)
  const canSwitchDraft = useCallback(
    () => pendingOperationsRef.current.size === 0,
    [],
  )

  const onDraftCreated = useCallback(
    (id: string) => {
      setSearchParams(
        (previous) => {
          const next = new URLSearchParams(previous)
          next.set('draft', id)
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  const {
    draft,
    setDraft,
    draftId,
    draftGeneration,
    isDraftGenerationCurrent,
    saveState,
    loading,
    loadFailure,
    isLoadFailureCurrent,
    loadDraft,
    saveNow,
    deleteCurrentDraft,
    deleteDraftById,
  } = useDraft({
    initialDraftId: urlDraftId,
    onDraftCreated,
    canSwitchDraft,
  })

  const launch = useWizardLaunch()
  const [finalizing, setFinalizing] = useState(false)
  const draftsList = useDraftsList(undefined, { enabled: urlDraftId === null && draftId === null })
  const resumeEligible = draftId === null && saveState === 'idle'
  const showResumePicker = resumeEligible && (draftsList.data?.length ?? 0) > 0
  const showResumeError = resumeEligible && draftsList.isError && !draftsList.data
  const maxContextPackets =
    authState?.status === 'authenticated'
      ? authState.systemInfo.limits.max_context_packets
      : undefined
  const canOperate =
    authState === undefined ||
    (authState.status === 'authenticated' && roleAtLeast(authState.consumer.role, 'operator'))
  const consumer =
    authState === undefined
      ? undefined
      : authState.status === 'authenticated'
        ? authState.consumer
        : null
  const scopeAuthorized =
    consumer === undefined ||
    canMutateAudience(consumer, draft.scope.project_id.trim(), draft.scope.app_id)
  const issues = [
    ...allIssues(draft, maxContextPackets),
    ...(!scopeAuthorized
      ? [
          {
            step: 'scope' as const,
            message: 'Select a project and application within your authorized scope',
          },
        ]
      : []),
  ]
  const canSaveDraft = canPersistWizardDraft(consumer, draft)
  const resetLaunch = launch.reset
  const beginStepPending = useCallback(
    (step: PendingWizardStep, operationDraftGeneration: number) => {
      if (!isDraftGenerationCurrent(operationDraftGeneration)) {
        return () => undefined
      }
      const operationId = ++nextPendingOperationIdRef.current
      pendingOperationsRef.current.set(operationId, {
        step,
        draftGeneration: operationDraftGeneration,
      })
      if (mountedRef.current) {
        setPendingStepCount(pendingOperationsRef.current.size)
      }
      let finished = false
      return () => {
        if (finished) return
        finished = true
        pendingOperationsRef.current.delete(operationId)
        if (mountedRef.current) {
          setPendingStepCount(pendingOperationsRef.current.size)
        }
      }
    },
    [isDraftGenerationCurrent],
  )

  const updateDraft = useCallback(
    (updater: DraftUpdater) => {
      setDraft(updater)
      if (launch.isError) resetLaunch()
    },
    [launch.isError, resetLaunch, setDraft],
  )

  useEffect(() => {
    if (
      !loadFailure ||
      !isLoadFailureCurrent(loadFailure) ||
      loadFailure.urlDraftId !== urlDraftId ||
      urlDraftId === draftId
    ) {
      return
    }
    setSearchParams((previous) => {
      const next = new URLSearchParams(previous)
      if (draftId === null) next.delete('draft')
      else next.set('draft', draftId)
      return next
    }, { replace: true })
  }, [
    draftId,
    isLoadFailureCurrent,
    loadFailure,
    urlDraftId,
    setSearchParams,
  ])

  useEffect(() => {
    let removedStaleOperation = false
    for (const [operationId, operation] of pendingOperationsRef.current) {
      if (operation.draftGeneration === draftGeneration) continue
      pendingOperationsRef.current.delete(operationId)
      removedStaleOperation = true
    }
    if (removedStaleOperation) {
      setPendingStepCount(pendingOperationsRef.current.size)
    }
    setFinalizing(false)
    resetLaunch()
  }, [draftGeneration, resetLaunch])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  async function handleLaunch() {
    if (finalizing || pendingOperationsRef.current.size > 0) return
    setFinalizing(true)
    const keyRevision = getApiKeyRevision()
    const sessionRevision = getSessionRevision()
    const launchedDraftId = draftId
    const launchedDraftGeneration = draftGeneration
    try {
      const result = await launch.mutateAsync(draft)
      const sessionIsCurrent =
        keyRevision === getApiKeyRevision() &&
        sessionRevision === getSessionRevision()
      const draftIsCurrent =
        mountedRef.current && isDraftGenerationCurrent(launchedDraftGeneration)
      const isCurrent = sessionIsCurrent && draftIsCurrent
      if (sessionIsCurrent) {
        if (draftIsCurrent) void deleteCurrentDraft()
        else if (launchedDraftId !== null) void deleteDraftById(launchedDraftId)
      }
      if (!isCurrent) return
      navigate(`/runs/${result.threadId}?tab=log`)
    } catch {
      // launch.error renders inline below.
      if (
        mountedRef.current &&
        isDraftGenerationCurrent(launchedDraftGeneration) &&
        keyRevision === getApiKeyRevision() &&
        sessionRevision === getSessionRevision()
      ) {
        setFinalizing(false)
      }
    }
  }

  async function resumeDraft(id: string) {
    if (pendingOperationsRef.current.size > 0) return
    if (!(await loadDraft(id))) return
    setSearchParams(
      (previous) => {
        const next = new URLSearchParams(previous)
        next.set('draft', id)
        return next
      },
      { replace: true },
    )
  }

  function selectStep(step: WizardStepId) {
    setSearchParams(
      (previous) => {
        const next = new URLSearchParams(previous)
        next.set('step', step)
        return next
      },
      { replace: true },
    )
  }

  function jumpToTab(step: WizardStepId) {
    selectStep(step)
    afterNextPaint(() => {
      document.getElementById('wizard-tabs')?.scrollIntoView({
        behavior: 'smooth',
        block: 'start',
      })
    })
  }

  function moveTab(event: KeyboardEvent<HTMLButtonElement>, step: WizardStepId) {
    const index = WIZARD_STEPS.indexOf(step)
    const lastIndex = WIZARD_STEPS.length - 1
    const nextStep =
      event.key === 'ArrowRight'
        ? WIZARD_STEPS[index === lastIndex ? 0 : index + 1]!
        : event.key === 'ArrowLeft'
          ? WIZARD_STEPS[index === 0 ? lastIndex : index - 1]!
          : event.key === 'Home'
            ? WIZARD_STEPS[0]!
            : event.key === 'End'
              ? WIZARD_STEPS[lastIndex]!
              : null

    if (nextStep === null) return
    event.preventDefault()
    selectStep(nextStep)
    afterNextPaint(() => {
      document.getElementById(`wizard-tab-${nextStep}`)?.focus()
    })
  }

  function renderStepSection(step: WizardStepId) {
    const copy = WIZARD_SECTION_COPY[step]
    return (
      <WizardSection
        key={step}
        id={step}
        title={copy.title}
        description={copy.description}
        active={activeStep === step}
      >
        <WizardStepContent
          step={step}
          draft={draft}
          onChange={updateDraft}
          onEditStep={jumpToTab}
          draftGeneration={draftGeneration}
          isDraftGenerationCurrent={isDraftGenerationCurrent}
          onStepPendingStart={beginStepPending}
          maxContextPackets={maxContextPackets}
          disabled={finalizing || !canOperate}
        />
      </WizardSection>
    )
  }

  function renderTabs() {
    return (
      <div id="wizard-tabs" className="glass-panel wizard-tabs-shell">
        <div
          className="wizard-tabs"
          role="tablist"
          aria-label="New test groups"
          aria-orientation="horizontal"
        >
          {WIZARD_STEPS.map((step) => (
            <button
              key={step}
              id={`wizard-tab-${step}`}
              type="button"
              role="tab"
              className="wizard-tab"
              aria-selected={activeStep === step}
              aria-controls={`wizard-panel-${step}`}
              tabIndex={activeStep === step ? 0 : -1}
              onClick={() => selectStep(step)}
              onKeyDown={(event) => moveTab(event, step)}
            >
              {STEP_LABELS[step]}
            </button>
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="wizard-page animate-enter">
      {SAVE_LABELS[saveState] && (
        <div className="wizard-status-row">
          <span
            className={`topbar-meta-chip${saveState === 'error' ? ' danger' : saveState === 'saved' ? ' success' : ''}`}
            data-testid="draft-save-state"
          >
            {SAVE_LABELS[saveState]}
          </span>
        </div>
      )}

      {resumeEligible && draftsList.isError && draftsList.data && (
        <CachedDataWarning error={draftsList.error} onRetry={() => void draftsList.refetch()} />
      )}

      {(showResumePicker || showResumeError) && (
        <div className="glass-panel wizard-resume" data-testid="resume-draft-panel">
          {showResumeError ? (
            <>
              <span className="wizard-label">Resume draft</span>
              <div className="tonal-card danger" role="alert">
                Saved drafts unavailable: {draftsList.error.message}{' '}
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => void draftsList.refetch()}
                >
                  Retry
                </button>
              </div>
            </>
          ) : (
            <>
              <label className="wizard-label" htmlFor="wizard-resume-select">
                Resume draft
              </label>
              <select
                id="wizard-resume-select"
                className="field-select"
                value=""
                disabled={loading || pendingStepCount > 0}
                onChange={(event) => {
                  if (event.target.value) void resumeDraft(event.target.value)
                }}
              >
                <option value="">Start fresh or pick a saved draft…</option>
                {(draftsList.data ?? []).map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    {entry.title}
                  </option>
                ))}
              </select>
            </>
          )}
        </div>
      )}

      {loadFailure && (
        <p className="wizard-caption wizard-caption--danger" role="alert">
          {loadFailure.message}
        </p>
      )}

      {loading ? (
        <p className="wizard-caption">Loading draft…</p>
      ) : (
        <fieldset className="wizard-fieldset" disabled={finalizing || !canOperate}>
          <div className="wizard-stack">
            {renderTabs()}
            {WIZARD_STEPS.map(renderStepSection)}
          </div>
        </fieldset>
      )}

      <footer className="glass-panel wizard-footer">
        <div className="wizard-footer-copy">
          <strong>Launch Pipeline</strong>
          <span className="wizard-caption">
            {issues.length === 0
              ? pendingStepCount > 0
                ? 'Waiting for in-progress context changes to finish.'
                : 'All required sections look ready.'
              : `${issues.length} item${issues.length === 1 ? '' : 's'} still need attention.`}
          </span>
        </div>
        {issues.length > 0 && (
          <div className="wizard-inline-issues" role="alert">
            {issues.slice(0, 3).map((issue) => (
              <button
                key={`${issue.step}:${issue.message}`}
                type="button"
                className="wizard-issue-link"
                onClick={() => jumpToTab(issue.step)}
              >
                {STEP_LABELS[issue.step]}: {issue.message}
              </button>
            ))}
          </div>
        )}
        {launch.isError && (
          <span className="wizard-caption wizard-caption--danger" role="alert">
            Launch failed: {launch.error.message}
          </span>
        )}
        <div className="wizard-footer-actions">
          <RequireRole role="operator">
            {!canSaveDraft && (
              <span className="wizard-caption">
                Draft saving requires project-wide access.
              </span>
            )}
            <button
              type="button"
              className="btn btn-ghost"
              disabled={loading || finalizing || !canSaveDraft}
              onClick={() => void saveNow()}
            >
              Save Draft
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={
                issues.length > 0 ||
                pendingStepCount > 0 ||
                launch.isPending ||
                loading ||
                finalizing
              }
              onClick={() => void handleLaunch()}
            >
              {launch.isPending || finalizing
                ? 'Launching…'
                : pendingStepCount > 0
                  ? 'Finishing context…'
                  : 'Launch Pipeline'}
            </button>
          </RequireRole>
        </div>
      </footer>
    </div>
  )
}
