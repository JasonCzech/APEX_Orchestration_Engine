import { promptContextEdits, type ContextPrompt } from '../prompts/promptContextEdits'
import { useTimestampPreference } from '@/theme/preferences'
import { StateCard } from '@/components/ux/StateCard'
import { Skeleton } from '@/components/ux/Skeleton'
import { useEffect, useMemo, useRef, useState } from 'react'

import { Link } from 'react-router'

import type { PhaseName, PipelineState } from '@apex/pipeline-events'

import {
  promptReviewWriteMutationKey,
  usePromptReview,
  useUpdatePromptReview,
  type PhasePromptReview,
} from '@/api/hooks/usePromptReview'
import { usePendingMutationCount } from '@/api/hooks/usePendingMutationCount'

import { promptPath } from '../prompts/promptPaths'
import { useOptionalConsumer } from '@/auth/AuthProvider'
import { roleAtLeast } from '@/auth/RequireRole'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { promptSourceLabel, promptSourceTone } from './promptProvenance'
import { formatTimestamp, PHASE_LABELS } from './runDisplay'
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
      system_locked: review.system_locked,
      contexts: review.contexts,
      selections: review.selections,
      system: review.system,
      phase_prompt: review.phase_prompt,
      application: review.system_locked ? review.application ?? null : appOverride ?? review.application ?? null,
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
      system_locked: prompt.system_locked,
      contexts: prompt.contexts,
      selections: prompt.selections,
      system: prompt.system ?? '',
      phase_prompt: prompt.user ?? '',
      application: prompt.system_locked ? prompt.application ?? null : appOverride ?? prompt.application ?? null,
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
    ...(review.system_locked ? { context_edits: promptContextEdits((review.contexts ?? []) as unknown as ContextPrompt[]) } : {}),
  }
}

function sameValues(left: PromptTabValues | null, right: PromptTabValues | null): boolean {
  if (!left || !right) return left === right
  return (
    left.system === right.system &&
    left.phase_prompt === right.phase_prompt &&
    (left.application ?? '') === (right.application ?? '') &&
    left.additional_context === right.additional_context &&
    JSON.stringify(left.context_edits) === JSON.stringify(right.context_edits)
  )
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
  useTimestampPreference()
  const snapshotReview = useMemo(() => reviewFromState(state, phase, appId), [state, phase, appId])
  const query = usePromptReview(threadId, phase)
  const update = useUpdatePromptReview(threadId)
  const writeCount = usePendingMutationCount(promptReviewWriteMutationKey(threadId))
  const writePending = writeCount > 0
  const resetUpdate = update.reset
  const consumer = useOptionalConsumer()
  const canEdit = consumer === undefined || (consumer !== null && roleAtLeast(consumer.role, 'operator'))
  const baseline = query.data ?? snapshotReview
  const baselineValues = useMemo(() => (baseline ? valuesOf(baseline) : null), [baseline])
  const [draft, setDraft] = useState<PromptTabValues | null>(baselineValues)
  const [saved, setSaved] = useState(false)
  const [userTouched, setUserTouched] = useState(false)
  const [activeTab, setActiveTab] = useState<PromptTabField>('system')
  const identityRef = useRef(JSON.stringify([threadId, phase]))
  const saveAttemptRef = useRef(0)
  const dirty = !sameValues(draft, baselineValues)
  const selectedSource = baseline?.selections?.find(item => item.namespace === (activeTab === 'phase_prompt' ? 'phase' : activeTab))
  const catalogLink = baseline?.system_locked
    ? selectedSource && typeof selectedSource.namespace === 'string' && typeof selectedSource.key === 'string' ? promptPath(selectedSource.namespace, selectedSource.key) : null
    : catalogLinkFor(activeTab, phase, appId)

  useEffect(() => {
    if (!userTouched) setDraft(baselineValues)
  }, [baselineValues, userTouched])

  useEffect(() => {
    identityRef.current = JSON.stringify([threadId, phase])
    saveAttemptRef.current += 1
    resetUpdate()
    setSaved(false)
    setUserTouched(false)
  }, [threadId, phase, resetUpdate])

  const appAvailable = Boolean(appId)

  function save() {
    if (!canEdit || !draft || !dirty || writePending) return
    const identity = JSON.stringify([threadId, phase])
    const attempt = ++saveAttemptRef.current
    update.mutate(
      {
        threadId,
        phase,
        body: {
          system: draft.system,
          phase_prompt: draft.phase_prompt,
          ...(!baseline?.system_locked ? { application: appAvailable ? draft.application ?? '' : null } : {}),
          ...(baseline?.system_locked ? { context_edits: draft.context_edits ?? promptContextEdits((baseline.contexts ?? []) as unknown as ContextPrompt[]) } : {}),
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
            <span className={`topbar-meta-chip prompt-provenance-chip ${promptSourceTone(baseline.source)}`} title={promptSourceLabel(baseline.source)} data-testid="prompt-review-source">
              {promptSourceLabel(baseline.source)}
            </span>
          )}
          {baseline?.updated_at && <span className="ux-context">Updated {formatTimestamp(baseline.updated_at)}{baseline.updated_by ? ` by ${baseline.updated_by}` : ''}</span>}
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
            disabled={!dirty || writePending}
            onClick={revert}
          >
            Revert
          </button>}
          {canEdit && <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={!dirty || writePending || !draft}
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

      {query.isError && baseline && (
        <CachedDataWarning error={query.error} onRetry={() => void query.refetch()} />
      )}

      {update.isError && (
        <div className="tonal-card danger" role="alert">
          {update.error instanceof Error ? update.error.message : 'Prompt review could not be saved.'}
        </div>
      )}

      {draft ? (
        <PromptTabsEditor
          values={draft}
          systemLocked={baseline?.system_locked === true}
          selections={baseline?.selections ?? undefined}
          contexts={baseline?.contexts as import('./PromptTabsEditor').PromptContextEntry[] | undefined}
          editable={canEdit && !writePending}
          appAvailable={appAvailable}
          active={activeTab}
          onActiveChange={setActiveTab}
          onContextChange={context_edits => {
            setSaved(false)
            setUserTouched(true)
            setDraft(previous => previous ? { ...previous, context_edits } : previous)
          }}
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
        query.isPending ? <Skeleton label="Loading prompt review" rows={2} /> :
          <StateCard kind="empty" compact title="No prompt review available." action={null} />
      )}
    </section>
  )
}
