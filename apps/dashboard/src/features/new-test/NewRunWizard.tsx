import { useTimestampPreference } from '@/theme/preferences'
import { formatTimestamp } from '@/utils/time'
import { ScopeContext } from '@/components/ux/ScopeContext'
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'

import { isApiError } from '@/api/errors'
import { useDraftsList } from '@/api/hooks/useDrafts'
import { useOptionalAuth } from '@/auth/AuthProvider'
import { getApiKeyRevision, getSessionRevision } from '@/auth/keyStorage'
import {
  canMutateAudience,
  pipelineLaunchRefusal,
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
import { useWizardReadiness } from './useWizardReadiness'
import {
  issueFieldId,
  wizardStepFromUrl,
  wizardStepUrl,
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
  onNormalize?: (updater: import('./useDraft').DraftUpdater) => void
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

function focusWizardTarget(id: string) {
  // A field can answer several readiness messages; secondary targets use an alias.
  const target = document.getElementById(id) ?? document.querySelector<HTMLElement>(`[data-focus-alias~="${CSS.escape(id)}"]`)
  target?.focus({ preventScroll: true })
  // The sticky footer can cover a field that is only scrolled to the nearest
  // viewport edge. Center both same-step and cross-step issue destinations.
  target?.scrollIntoView?.({ block: 'center', inline: 'nearest' })
}

const REQUIRED_STEPS = new Set<WizardStepId>(['scope', 'config', 'prompts'])

function issueCountLabel(count: number) {
  return `${count} item${count === 1 ? ' needs' : 's need'} attention`
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
          <h2 className="wizard-section-title">{title}</h2>
        </div>
        <p className="wizard-section-description">{description}</p>
      </div>
      {children}
    </section>
  )
}

const WIZARD_SECTION_COPY: Record<WizardStepId, { title: string; description: string }> = {
  scope: {
    title: 'Test scope',
    description:
      'Name the test, describe the request, and define the systems under test and where traffic enters.',
  },
  'work-items': {
    title: 'Tickets',
    description:
      'Attach tickets or stories that should anchor the test context and downstream reporting.',
  },
  context: {
    title: 'Knowledge',
    description:
      'Add documents so the phases have the evidence they need before execution starts.',
  },
  config: {
    title: 'Execution configuration',
    description:
      'Choose the engine, phase ownership, analysis tools, and agent review policy. Phases a person completes pause until they submit the result.',
  },
  prompts: {
    title: 'Prompt selection',
    description:
      'Choose the shared system prompt, required phase context, and optional application and environment context.',
  },
  review: {
    title: 'Launch preview',
    description:
      'Review the launch plan. Selected tickets and documents are resolved into agent context when the test starts.',
  },
}

function WizardStepContent({
  step,
  draft,
  onChange,
  onNormalize,
  onEditStep,
  draftGeneration = 0,
  isDraftGenerationCurrent,
  onStepPendingStart,
  maxContextPackets,
  disabled,
}: StepProps & {
  step: WizardStepId
  onEditStep: (step: WizardStepId, field?: string) => void
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
      return <PromptsStep draft={draft} onChange={onChange} onNormalize={onNormalize} />
    case 'review':
      return <ReviewStep draft={draft} onChange={onChange} onEditStep={onEditStep} maxContextPackets={maxContextPackets} />
  }
}

export function NewRunWizardPage() {
  useTimestampPreference()
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
  const activeStep = wizardStepFromUrl(rawStep)
  useEffect(() => {
    if (rawStep !== 'work-items' && rawStep !== 'context') return
    setSearchParams(previous => { const next = new URLSearchParams(previous); next.set('step', wizardStepUrl(activeStep)); return next }, { replace: true })
  }, [rawStep, activeStep, setSearchParams])
  const pendingFocusRef = useRef<{ step: WizardStepId; id: string; previous: Element | null } | null>(null)
  useEffect(() => {
    const target = pendingFocusRef.current
    if (target?.step !== activeStep) return
    pendingFocusRef.current = null
    if (document.activeElement !== target.previous && document.activeElement !== document.body) return
    focusWizardTarget(target.id)
  }, [activeStep])
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
    normalizeDraft,
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
    (draft.scope.test_scope ? draft.scope.test_scope.participants.every(p => canMutateAudience(consumer, p.project_id, p.application_id)) : canMutateAudience(consumer, draft.scope.project_id.trim(), draft.scope.app_id))
  const { readiness, environment: targetEnvironment, execution, executionVerificationEnabled } = useWizardReadiness(draft, maxContextPackets)
  const launchRefusal = consumer === undefined ? null : pipelineLaunchRefusal(consumer)
  const issues = [
    ...readiness.planningIssues,
    ...readiness.executionIssues,
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
    if (finalizing || issues.length > 0 || !canOperate || pendingOperationsRef.current.size > 0) return
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
      navigate(`/runs/${result.threadId}`)
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
        next.set('step', wizardStepUrl(step))
        return next
      },
      { replace: true },
    )
  }

  function jumpToTab(step: WizardStepId, field?: string) {
    const id = field ?? `wizard-tab-${step}`
    if (step === activeStep) focusWizardTarget(id)
    else pendingFocusRef.current = { step, id, previous: document.activeElement }
    selectStep(step)
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
    jumpToTab(nextStep)
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
        {(step !== 'review' || activeStep === 'review') && <WizardStepContent
          step={step}
          draft={draft}
          onChange={updateDraft}
          onNormalize={normalizeDraft}
          onEditStep={jumpToTab}
          draftGeneration={draftGeneration}
          isDraftGenerationCurrent={isDraftGenerationCurrent}
          onStepPendingStart={beginStepPending}
          maxContextPackets={maxContextPackets}
          disabled={finalizing || !canOperate}
        />}
      </WizardSection>
    )
  }

  function renderTabs() {
    return (
      <div id="wizard-tabs" className="glass-panel wizard-tabs-shell">
        <div
          className="wizard-tabs workspace-tabs ux-underline-tabs"
          role="tablist"
          aria-label="New test groups"
          aria-orientation="horizontal"
        >
          {WIZARD_STEPS.map((step) => {
            // Each tab carries its own state so the stepper, not only the
            // footer, shows where work remains. Optional steps stay unmarked.
            const open = step === 'review' ? 0 : issues.filter(issue => issue.step === step).length
            const done = open === 0 && REQUIRED_STEPS.has(step)
            return (
              <button
                key={step}
                id={`wizard-tab-${step}`}
                type="button"
                role="tab"
                aria-label={STEP_LABELS[step]}
                aria-describedby={open || done ? `wizard-tab-${step}-state` : undefined}
                className="wizard-tab workspace-tab"
                aria-selected={activeStep === step}
                aria-controls={`wizard-panel-${step}`}
                tabIndex={activeStep === step ? 0 : -1}
                onClick={() => selectStep(step)}
                onKeyDown={(event) => moveTab(event, step)}
              >
                {WIZARD_STEPS.indexOf(step) + 1}. {STEP_LABELS[step]}
                {open > 0 && <span className="wizard-tab-state attention" aria-hidden="true">{open}</span>}
                {done && <span className="wizard-tab-state done" aria-hidden="true">✓</span>}
                {(open > 0 || done) && <span id={`wizard-tab-${step}-state`} className="sr-only">{open > 0 ? issueCountLabel(open) : 'Complete'}</span>}
              </button>
            )
          })}
        </div>
      </div>
    )
  }

  const readinessSummary = issues.length === 0
    ? pendingStepCount > 0 ? 'Finishing context changes…' : 'Ready to launch'
    : readiness.planningIssues.length === 0
      ? `Execution setup: ${issueCountLabel(issues.length)}`
      : issueCountLabel(issues.length)
  const saveDraftNote = canSaveDraft
    ? null
    : draft.scope.project_id.trim()
      ? 'Draft saving requires project-wide access.'
      : 'Choose a primary system to save a draft.'

  return (
    <div className="wizard-page animate-enter">
      {launchRefusal && <p className="tonal-card warning" role="alert">{launchRefusal}</p>}
      <ScopeContext project={draft.scope.project_id} application={draft.scope.app_id} environment={draft.scope.environment_id} />

      {SAVE_LABELS[saveState] && (
        <div className="wizard-status-row">
          <span
            className={`topbar-meta-chip${saveState === 'error' ? ' danger' : saveState === 'saved' ? ' success' : ''}`}
            data-testid="draft-save-state"
            role="status"
            aria-live="polite"
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
                    {entry.title || 'Untitled draft'} · {entry.project_id || 'No project'} · {entry.updated_at ? formatTimestamp(entry.updated_at) : entry.id}
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

      {launch.isError && <div className="tonal-card danger" role="alert">
        <strong>Launch failed: {launch.error.message}</strong>
        {/prompt_selections|selected prompt/i.test(launch.error.message) && <button type="button" className="btn btn-secondary" onClick={() => jumpToTab('prompts')}>Review prompt selections</button>}
        {/environment|target/i.test(launch.error.message) && draft.scope.environment_id && <p>Environment: {targetEnvironment.data?.name ?? draft.scope.environment_id}. <Link to={`/environments/${encodeURIComponent(draft.scope.environment_id)}`}>Review environment target</Link>.</p>}
        <p>Retrying unchanged inputs on this page reuses the same launch request. Recovery may be blocked after a reload or credential change; check Tests before starting another request. Editing the inputs creates a different launch request.</p>
        {isApiError(launch.error) && <details><summary>Technical details</summary><p>HTTP status: {launch.error.status}</p></details>}
      </div>}
      {/* One compact row: the step, a readiness chip that discloses the issue
          list on demand, and the actions. Server-recheck caveats live on Review. */}
      <footer className="glass-panel wizard-footer">
        <div className="wizard-footer-copy">
          <strong>{activeStep === 'review' ? 'Review before launch' : `Step ${WIZARD_STEPS.indexOf(activeStep) + 1} of ${WIZARD_STEPS.length} · ${STEP_LABELS[activeStep]}`}</strong>
          {/* One always-mounted live region announces readiness changes; the
              visible status below is decoration for sighted users. */}
          <span className="sr-only" role="status">{readinessSummary}</span>
          {issues.length === 0 ? (
            <span className={`wizard-footer-status${pendingStepCount > 0 ? '' : ' ready'}`}>
              {pendingStepCount > 0 ? 'Finishing context changes…' : <><span aria-hidden="true">✓ </span>Ready to launch</>}
            </span>
          ) : activeStep === 'review' ? (
            <span className="wizard-footer-status attention">{readinessSummary}</span>
          ) : (
            <details
              key={activeStep}
              className="wizard-footer-issues"
              onKeyDown={(event) => {
                if (event.key !== 'Escape' || !event.currentTarget.open) return
                event.currentTarget.open = false
                event.currentTarget.querySelector('summary')?.focus()
              }}
            >
              <summary className="wizard-footer-status attention">{readinessSummary}</summary>
              <ul className="wizard-inline-issues" aria-label="Items needing attention">
                {issues.map(issue => <li key={`${issue.step}:${issue.message}`}><button type="button" className="wizard-issue-link" onClick={(event) => { event.currentTarget.closest('details')?.removeAttribute('open'); jumpToTab(issue.step, issueFieldId(issue.message)) }}>{STEP_LABELS[issue.step]}: {issue.message}</button></li>)}
              </ul>
            </details>
          )}
          {targetEnvironment.isError && draft.scope.environment_id && <button type="button" className="btn btn-secondary btn-sm" onClick={() => void targetEnvironment.refetch()}>Retry environment verification</button>}
          {executionVerificationEnabled && (execution.isError || execution.data?.configuration_valid === false) && activeStep !== 'review' && <button type="button" className="btn btn-secondary btn-sm" disabled={execution.isFetching} onClick={() => void execution.refetch()}>Retry execution verification</button>}
        </div>

        <div className="wizard-footer-actions">
          <RequireRole role="operator">
            {saveDraftNote && <span id="wizard-save-note" className="wizard-caption">{saveDraftNote}</span>}
            <span id="wizard-save-hint" className="sr-only">Saving does not launch a test.</span>
            <button
              type="button"
              className="btn btn-ghost"
              disabled={loading || finalizing || !canSaveDraft}
              aria-describedby={saveDraftNote ? 'wizard-save-note' : 'wizard-save-hint'}
              onClick={() => void saveNow()}
            >
              Save draft
            </button>
            {WIZARD_STEPS.indexOf(activeStep) > 0 && <button type="button" className="btn btn-secondary" disabled={loading || finalizing || pendingStepCount > 0} onClick={() => jumpToTab(WIZARD_STEPS[WIZARD_STEPS.indexOf(activeStep) - 1]!)}>Back</button>}
            <button
              type="button"
              className="btn btn-primary"
              disabled={
                (activeStep === 'review' && issues.length > 0) ||
                pendingStepCount > 0 ||
                launch.isPending ||
                loading ||
                finalizing
              }
              onClick={() => activeStep === 'review' ? void handleLaunch() : jumpToTab(WIZARD_STEPS[WIZARD_STEPS.indexOf(activeStep) + 1]!)}
            >
              {launch.isPending || finalizing
                ? 'Launching…'
                : pendingStepCount > 0
                  ? 'Finishing context…'
                  : activeStep === 'review' ? launch.isError ? 'Retry launch' : 'Launch test' : activeStep === 'prompts' ? 'Review test' : 'Next'}
            </button>
          </RequireRole>
        </div>
      </footer>
    </div>
  )
}
