/**
 * NewRunWizard — the 6-step /runs/new wizard (plan Part 2 UX 2.c + section 4).
 *
 * Layout: full-page left step rail + content + sticky summary footer.
 * URL contract: /runs/new?step=scope|work-items|context|config|prompts|review
 * &draft=<id> — step changes replace history; the draft id lands in the URL
 * after the first autosave creates it. A "Resume draft" picker shows on first
 * visit when server drafts exist.
 */
import { useCallback, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router'

import { useDraftsList } from '@/api/hooks/useDrafts'

import { ConfigStep } from './steps/ConfigStep'
import { ContextStep } from './steps/ContextStep'
import { PromptsStep } from './steps/PromptsStep'
import { ReviewStep } from './steps/ReviewStep'
import { ScopeStep } from './steps/ScopeStep'
import { WorkItemsStep } from './steps/WorkItemsStep'
import { useDraft, type DraftUpdater } from './useDraft'
import { useWizardLaunch } from './useWizardLaunch'
import {
  allIssues,
  isStepValid,
  isWizardStep,
  STEP_LABELS,
  WIZARD_STEPS,
  type WizardDraft,
  type WizardStepId,
} from './wizardState'

import './wizard.css'

/** Props every step receives: the draft plus the autosaving updater. */
export interface StepProps {
  draft: WizardDraft
  onChange: (updater: DraftUpdater) => void
}

const SAVE_LABELS: Record<string, string> = {
  idle: '',
  pending: 'Unsaved changes',
  saving: 'Saving…',
  saved: 'Draft saved',
  error: 'Draft save failed',
}

export function NewRunWizardPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()

  const stepParam = searchParams.get('step')
  const step: WizardStepId = isWizardStep(stepParam) ? stepParam : 'scope'
  const urlDraftId = searchParams.get('draft')

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

  // Resume entry point: only on a fresh visit (no draft yet, nothing typed).
  const draftsList = useDraftsList(undefined, { enabled: urlDraftId === null && draftId === null })
  const showResume =
    draftId === null && saveState === 'idle' && (draftsList.data?.length ?? 0) > 0

  const [visited, setVisited] = useState<Set<WizardStepId>>(() => new Set([step]))

  const goToStep = useCallback(
    (next: WizardStepId) => {
      setVisited((previous) => new Set(previous).add(next))
      setSearchParams(
        (previous) => {
          const params = new URLSearchParams(previous)
          params.set('step', next)
          return params
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  const stepIndex = WIZARD_STEPS.indexOf(step)
  const currentValid = isStepValid(draft, step)
  const issues = allIssues(draft)

  async function handleLaunch() {
    try {
      const result = await launch.mutateAsync(draft)
      await deleteCurrentDraft()
      navigate(`/runs/${result.threadId}?tab=activity`)
    } catch {
      // launch.error renders below; stay on the review step.
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

  return (
    <div className="wizard-page animate-enter">
      <header className="wizard-header">
        <h2 className="wizard-title">New run</h2>
        {SAVE_LABELS[saveState] && (
          <span
            className={`topbar-meta-chip${saveState === 'error' ? ' danger' : saveState === 'saved' ? ' success' : ''}`}
            data-testid="draft-save-state"
          >
            {SAVE_LABELS[saveState]}
          </span>
        )}
      </header>

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

      <div className="wizard-body">
        <nav className="glass-panel wizard-rail" aria-label="Wizard steps">
          <ol>
            {WIZARD_STEPS.map((id, index) => {
              const isCurrent = id === step
              const isVisited = visited.has(id)
              const isComplete = isVisited && !isCurrent && isStepValid(draft, id)
              return (
                <li key={id}>
                  <button
                    type="button"
                    className={`wizard-rail-step${isCurrent ? ' wizard-rail-step--current' : ''}${
                      isComplete ? ' wizard-rail-step--complete' : ''
                    }`}
                    aria-current={isCurrent ? 'step' : undefined}
                    disabled={!isVisited && !isCurrent}
                    onClick={() => goToStep(id)}
                  >
                    <span className="wizard-rail-index">{isComplete ? '✓' : index + 1}</span>
                    <span>{STEP_LABELS[id]}</span>
                  </button>
                </li>
              )
            })}
          </ol>
        </nav>

        <div className="wizard-content">
          {loading ? (
            <p className="wizard-caption">Loading draft…</p>
          ) : (
            <>
              {step === 'scope' && <ScopeStep draft={draft} onChange={setDraft} />}
              {step === 'work-items' && <WorkItemsStep draft={draft} onChange={setDraft} />}
              {step === 'context' && <ContextStep draft={draft} onChange={setDraft} />}
              {step === 'config' && <ConfigStep draft={draft} onChange={setDraft} />}
              {step === 'prompts' && <PromptsStep draft={draft} onChange={setDraft} />}
              {step === 'review' && <ReviewStep draft={draft} onEditStep={goToStep} />}
            </>
          )}
        </div>
      </div>

      <footer className="glass-panel wizard-footer">
        <button
          type="button"
          className="btn btn-ghost"
          disabled={stepIndex === 0}
          onClick={() => goToStep(WIZARD_STEPS[stepIndex - 1] as WizardStepId)}
        >
          Back
        </button>
        <button type="button" className="btn btn-ghost" onClick={() => void saveNow()}>
          Save draft
        </button>
        <span className="wizard-footer-spacer" />
        {launch.isError && (
          <span className="wizard-caption wizard-caption--danger" role="alert">
            Launch failed: {launch.error.message}
          </span>
        )}
        {step !== 'review' ? (
          <button
            type="button"
            className="btn btn-primary"
            disabled={!currentValid || loading}
            onClick={() => goToStep(WIZARD_STEPS[stepIndex + 1] as WizardStepId)}
          >
            Next
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-primary"
            disabled={issues.length > 0 || launch.isPending || loading}
            onClick={() => void handleLaunch()}
          >
            {launch.isPending ? 'Launching…' : 'Launch'}
          </button>
        )}
      </footer>
    </div>
  )
}
