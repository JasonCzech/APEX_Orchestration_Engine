import { useEffect, useMemo, useRef, useState } from 'react'

import { Link } from 'react-router'

import type { PhaseName, PipelineState } from '@apex/pipeline-events'

import {
  usePromptReview,
  useUpdatePromptReview,
  type PhasePromptReview,
} from '@/api/hooks/usePromptReview'

import { promptPath } from '../prompts/promptPaths'
import { useOptionalConsumer } from '@/auth/AuthProvider'
import { roleAtLeast } from '@/auth/RequireRole'
import { PHASE_LABELS } from './runDisplay'
import { PromptTabsEditor, type PromptTabField, type PromptTabValues } from './PromptTabsEditor'

/** Run-scoped, app-wide application prompt override content, if set for this run's app. */
function appOverrideContent(state: PipelineState, appId: string | null): string | null {
  if (!appId) return null
  const override = state.application_reviews?.[appId]
  return override && override.content != null ? override.content : null
}

function reviewFromState(
  state: PipelineState,
  phase: PhaseName,
  appId: string | null,
): PhasePromptReview | null {
  const appOverride = appOverrideContent(state, appId)
  const review = state.prompt_reviews?.[phase]
  if (review) {
    return {
      system: review.system,
      phase_prompt: review.phase_prompt,
      application: appOverride ?? review.application ?? null,
      additional_context: review.additional_context,
      source: review.source ?? {},
      updated_at: review.updated_at,
      updated_by: review.updated_by,
    }
  }
  const entry = state.phase_results?.[phase]
  const prompt = entry?.resolved_prompt
  if (prompt?.system || prompt?.user || prompt?.application) {
    return {
      system: prompt.system ?? '',
      phase_prompt: prompt.user ?? '',
      application: appOverride ?? prompt.application ?? null,
      additional_context: '',
      source: entry?.resolved_prompt_source ?? {},
      updated_at: entry?.started_at ?? '',
      updated_by: 'system',
    }
  }
  return null
}

/** Catalog deep-link for the active tab, or null when it has no catalog entry. */
function catalogLinkFor(
  active: PromptTabField,
  phase: PhaseName,
  appId: string | null,
): string | null {
  switch (active) {
    case 'system':
      return promptPath('phase', `${phase}/system`)
    case 'phase_prompt':
      return promptPath('phase', `${phase}/user`)
    case 'application':
      return appId ? promptPath('application', appId) : null
    default:
      return null
  }
}

function valuesOf(review: PhasePromptReview): PromptTabValues {
  return {
    system: review.system,
    phase_prompt: review.phase_prompt,
    application: review.application ?? null,
    additional_context: review.additional_context,
  }
}

function sameValues(left: PromptTabValues | null, right: PromptTabValues | null): boolean {
  if (!left || !right) return left === right
  return (
    left.system === right.system &&
    left.phase_prompt === right.phase_prompt &&
    (left.application ?? '') === (right.application ?? '') &&
    left.additional_context === right.additional_context
  )
}

function sourceLabel(source: Record<string, unknown>): string {
  const origin = typeof source.origin === 'string' ? source.origin : 'runtime'
  const ref = typeof source.ref === 'string' && source.ref ? ` · ${source.ref}` : ''
  return `${origin}${ref}`
}

export function PromptReviewSection({
  threadId,
  phase,
  state,
  appId,
}: {
  threadId: string
  phase: PhaseName
  state: PipelineState
  appId: string | null
}) {
  const snapshotReview = useMemo(() => reviewFromState(state, phase, appId), [state, phase, appId])
  const query = usePromptReview(threadId, phase)
  const update = useUpdatePromptReview()
  const consumer = useOptionalConsumer()
  const canEdit = consumer === undefined || (consumer !== null && roleAtLeast(consumer.role, 'operator'))
  const baseline = query.data ?? snapshotReview
  const baselineValues = useMemo(() => (baseline ? valuesOf(baseline) : null), [baseline])
  const [draft, setDraft] = useState<PromptTabValues | null>(baselineValues)
  const [saved, setSaved] = useState(false)
  const [userTouched, setUserTouched] = useState(false)
  const [activeTab, setActiveTab] = useState<PromptTabField>('system')
  const identityRef = useRef(`${threadId}:${phase}`)
  const saveAttemptRef = useRef(0)
  const dirty = !sameValues(draft, baselineValues)
  const catalogLink = catalogLinkFor(activeTab, phase, appId)

  useEffect(() => {
    if (!userTouched) setDraft(baselineValues)
  }, [baselineValues, userTouched])

  useEffect(() => {
    identityRef.current = `${threadId}:${phase}`
    saveAttemptRef.current += 1
    setSaved(false)
    setUserTouched(false)
  }, [threadId, phase])

  const appAvailable = Boolean(appId)

  function save() {
    if (!canEdit || !draft || !dirty || update.isPending) return
    const identity = `${threadId}:${phase}`
    const attempt = ++saveAttemptRef.current
    update.mutate(
      {
        threadId,
        phase,
        body: {
          system: draft.system,
          phase_prompt: draft.phase_prompt,
          application: appAvailable ? draft.application ?? '' : null,
          additional_context: draft.additional_context,
        },
      },
      {
        onSuccess: (next) => {
          if (identityRef.current !== identity || saveAttemptRef.current !== attempt) return
          setDraft(valuesOf(next))
          setUserTouched(false)
          setSaved(true)
        },
      },
    )
  }

  function revert() {
    setDraft(baselineValues)
    setUserTouched(false)
    setSaved(false)
  }

  return (
    <section
      className="prompt-review-section"
      aria-label={`${PHASE_LABELS[phase]} Prompt Review`}
      data-testid="prompt-review-section"
    >
      <header className="prompt-review-head">
        <div>
          <h3 className="prompt-review-title">Prompt Review</h3>
          <p className="prompt-review-hint">
            Run-scoped only — permanent edits are made in the{' '}
            <Link to="/prompts?ns=phase">Prompt Catalog</Link>.
          </p>
        </div>
        <div className="prompt-review-actions">
          {baseline?.source && (
            <span className="topbar-meta-chip accent" data-testid="prompt-review-source">
              {sourceLabel(baseline.source)}
            </span>
          )}
          {dirty && <span className="topbar-meta-chip warning">edited</span>}
          {saved && !dirty && <span className="topbar-meta-chip success">saved</span>}
          {catalogLink && (
            <Link className="btn btn-ghost btn-sm" to={catalogLink}>
              Catalog
            </Link>
          )}
          {canEdit && <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={!dirty || update.isPending}
            onClick={revert}
          >
            Revert
          </button>}
          {canEdit && <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={!dirty || update.isPending || !draft}
            onClick={save}
          >
            {update.isPending ? 'Saving…' : 'Save to run'}
          </button>}
        </div>
      </header>

      {query.isError && !baseline && (
        <div className="tonal-card danger" role="alert">
          {query.error instanceof Error ? query.error.message : 'Prompt review could not load.'}
        </div>
      )}

      {draft ? (
        <PromptTabsEditor
          values={draft}
          editable={canEdit && !update.isPending}
          appAvailable={appAvailable}
          active={activeTab}
          onActiveChange={setActiveTab}
          onChange={(field, value) => {
            setSaved(false)
            setUserTouched(true)
            setDraft((prev) => {
              const current = prev ?? {
                system: '',
                phase_prompt: '',
                application: null,
                additional_context: '',
              }
              return { ...current, [field]: value }
            })
          }}
        />
      ) : (
        <div className="dash-empty small" aria-busy={query.isPending}>
          {query.isPending ? 'Loading prompt review…' : 'No prompt review available.'}
        </div>
      )}
    </section>
  )
}
