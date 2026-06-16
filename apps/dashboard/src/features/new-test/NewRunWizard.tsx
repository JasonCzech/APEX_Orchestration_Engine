import { useCallback, type ReactNode } from 'react'
import { useNavigate, useSearchParams } from 'react-router'

import { useDraftsList } from '@/api/hooks/useDrafts'

import { ConfigStep } from './steps/ConfigStep'
import { ContextStep } from './steps/ContextStep'
import { PromptsStep } from './steps/PromptsStep'
import { ReviewStep } from './steps/ReviewStep'
import { ScopeStep } from './steps/ScopeStep'
import { WorkItemsStep } from './steps/WorkItemsStep'
import { useDraft } from './useDraft'
import { useWizardLaunch } from './useWizardLaunch'
import { allIssues, STEP_LABELS, type WizardStepId } from './wizardState'

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
  children,
}: {
  id: WizardStepId
  title: string
  description: string
  children: ReactNode
}) {
  return (
    <section id={`wizard-section-${id}`} className="glass-panel wizard-section" aria-label={title}>
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

export function NewRunWizardPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
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

  function jumpToSection(step: WizardStepId) {
    document.getElementById(`wizard-section-${step}`)?.scrollIntoView({
      behavior: 'smooth',
      block: 'start',
    })
  }

  return (
    <div className="wizard-page animate-enter">
      <header className="wizard-header">
        <div>
          <h2 className="wizard-title">New Test</h2>
          <p className="wizard-header-copy">
            Build the run in one pass, keep the draft autosaved, and launch the pipeline when the configuration looks right.
          </p>
        </div>
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

      {loading ? (
        <p className="wizard-caption">Loading draft…</p>
      ) : (
        <div className="wizard-stack">
          <WizardSection
            id="scope"
            title="Run Scope"
            description="Name the pipeline, describe the request, and choose the project, application, and target environment."
          >
            <ScopeStep draft={draft} onChange={setDraft} />
          </WizardSection>

          <WizardSection
            id="work-items"
            title="Work Items"
            description="Attach tickets or stories that should anchor the pipeline context and downstream reporting."
          >
            <WorkItemsStep draft={draft} onChange={setDraft} />
          </WizardSection>

          <WizardSection
            id="context"
            title="Context"
            description="Add documents and context summaries so the phases have the evidence they need before execution starts."
          >
            <ContextStep draft={draft} onChange={setDraft} />
          </WizardSection>

          <WizardSection
            id="config"
            title="Execution Configuration"
            description="Choose the engine, phase coverage, and review policy. Enable full manual step-through by keeping every phase gated."
          >
            <ConfigStep draft={draft} onChange={setDraft} />
          </WizardSection>

          <WizardSection
            id="prompts"
            title="Prompt Selection"
            description="Select the prompt set for each phase and override only the places that need run-specific instructions."
          >
            <PromptsStep draft={draft} onChange={setDraft} />
          </WizardSection>

          <WizardSection
            id="review"
            title="Launch Preview"
            description="Review the exact launch payload that will be sent when you start the pipeline."
          >
            <ReviewStep draft={draft} onEditStep={jumpToSection} />
          </WizardSection>
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
                onClick={() => jumpToSection(issue.step)}
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
        </div>
      </footer>
    </div>
  )
}
