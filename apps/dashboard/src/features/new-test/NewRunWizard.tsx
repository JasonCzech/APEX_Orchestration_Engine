import { useCallback, type KeyboardEvent, type ReactNode } from 'react'
import { useNavigate, useSearchParams } from 'react-router'

import { useDraftsList } from '@/api/hooks/useDrafts'
import { RequireRole } from '@/auth/RequireRole'

import { ConfigStep } from './steps/ConfigStep'
import { ContextStep } from './steps/ContextStep'
import { PromptsStep } from './steps/PromptsStep'
import { ReviewStep } from './steps/ReviewStep'
import { ScopeStep } from './steps/ScopeStep'
import { WorkItemsStep } from './steps/WorkItemsStep'
import { useDraft } from './useDraft'
import { useWizardLaunch } from './useWizardLaunch'
import { allIssues, isWizardStep, STEP_LABELS, WIZARD_STEPS, type WizardStepId } from './wizardState'

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
}: StepProps & { step: WizardStepId; onEditStep: (step: WizardStepId) => void }) {
  switch (step) {
    case 'scope':
      return <ScopeStep draft={draft} onChange={onChange} />
    case 'work-items':
      return <WorkItemsStep draft={draft} onChange={onChange} />
    case 'context':
      return <ContextStep draft={draft} onChange={onChange} />
    case 'config':
      return <ConfigStep draft={draft} onChange={onChange} />
    case 'prompts':
      return <PromptsStep draft={draft} onChange={onChange} />
    case 'review':
      return <ReviewStep draft={draft} onEditStep={onEditStep} />
  }
}

export function NewRunWizardPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const urlDraftId = searchParams.get('draft')
  const rawStep = searchParams.get('step')
  const activeStep: WizardStepId = isWizardStep(rawStep) ? rawStep : 'scope'

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
    saveState,
    loading,
    loadError,
    loadDraft,
    saveNow,
    deleteCurrentDraft,
  } = useDraft({ initialDraftId: urlDraftId, onDraftCreated })

  const launch = useWizardLaunch()
  const draftsList = useDraftsList(undefined, { enabled: urlDraftId === null && draftId === null })
  const showResume =
    draftId === null && saveState === 'idle' && (draftsList.data?.length ?? 0) > 0
  const issues = allIssues(draft)

  async function handleLaunch() {
    try {
      const result = await launch.mutateAsync(draft)
      await deleteCurrentDraft()
      navigate(`/runs/${result.threadId}?tab=log`)
    } catch {
      // launch.error renders inline below.
    }
  }

  async function resumeDraft(id: string) {
    await loadDraft(id)
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
          onChange={setDraft}
          onEditStep={jumpToTab}
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

      {showResume && (
        <div className="glass-panel wizard-resume" data-testid="resume-draft-panel">
          <label className="wizard-label" htmlFor="wizard-resume-select">
            Resume draft
          </label>
          <select
            id="wizard-resume-select"
            className="field-select"
            value=""
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
        </div>
      )}

      {loadError && (
        <p className="wizard-caption wizard-caption--danger" role="alert">
          {loadError}
        </p>
      )}

      {loading ? (
        <p className="wizard-caption">Loading draft…</p>
      ) : (
        <div className="wizard-stack">
          {renderTabs()}
          {WIZARD_STEPS.map(renderStepSection)}
        </div>
      )}

      <footer className="glass-panel wizard-footer">
        <div className="wizard-footer-copy">
          <strong>Launch Pipeline</strong>
          <span className="wizard-caption">
            {issues.length === 0
              ? 'All required sections look ready.'
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
            <button type="button" className="btn btn-ghost" onClick={() => void saveNow()}>
              Save Draft
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={issues.length > 0 || launch.isPending || loading}
              onClick={() => void handleLaunch()}
            >
              {launch.isPending ? 'Launching…' : 'Launch Pipeline'}
            </button>
          </RequireRole>
        </div>
      </footer>
    </div>
  )
}
