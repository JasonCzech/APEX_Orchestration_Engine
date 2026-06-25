import { useEffect, useMemo, useState } from 'react'

import { Link } from 'react-router'

import type { PhaseName, PipelineState } from '@apex/pipeline-events'

import {
  usePromptReview,
  useUpdatePromptReview,
  type PhasePromptReview,
} from '@/api/hooks/usePromptReview'

import { promptPath } from '../prompts/promptPaths'
import { PHASE_LABELS } from './runDisplay'
import { PromptTabsEditor, type PromptTabValues } from './PromptTabsEditor'

function reviewFromState(state: PipelineState, phase: PhaseName): PhasePromptReview | null {
  const review = state.prompt_reviews?.[phase]
  if (review) {
    return {
      system: review.system,
      phase_prompt: review.phase_prompt,
      application: review.application ?? null,
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
      application: prompt.application ?? null,
      additional_context: '',
      source: entry?.resolved_prompt_source ?? {},
      updated_at: entry?.started_at ?? '',
      updated_by: 'system',
    }
  }
  return null
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
  const snapshotReview = useMemo(() => reviewFromState(state, phase), [state, phase])
  const query = usePromptReview(threadId, phase)
  const update = useUpdatePromptReview()
  const baseline = query.data ?? snapshotReview
  const baselineValues = useMemo(() => (baseline ? valuesOf(baseline) : null), [baseline])
  const [draft, setDraft] = useState<PromptTabValues | null>(baselineValues)
  const [saved, setSaved] = useState(false)
  const [userTouched, setUserTouched] = useState(false)
  const dirty = !sameValues(draft, baselineValues)

  useEffect(() => {
    if (!userTouched) setDraft(baselineValues)
  }, [baselineValues, userTouched])

  useEffect(() => {
    setSaved(false)
    setUserTouched(false)
  }, [phase])

  const appAvailable = Boolean(appId)

  function save() {
    if (!draft || !dirty || update.isPending) return
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
          <Link
            className="btn btn-ghost btn-sm"
            to={promptPath('phase', `${phase}/system`)}
          >
            Catalog
          </Link>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={!dirty || update.isPending}
            onClick={revert}
          >
            Revert
          </button>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={!dirty || update.isPending || !draft}
            onClick={save}
          >
            {update.isPending ? 'Saving…' : 'Save to run'}
          </button>
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
          editable={!update.isPending}
          appAvailable={appAvailable}
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
              return {
                ...current,
                [field]: field === 'application' ? value : value,
              }
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
