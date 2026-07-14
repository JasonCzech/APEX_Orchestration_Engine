/**
 * Step 5 — Prompts: the focused phase's system prompt plus the selected
 * application's app-wide prompt. System overrides keep the historical
 * prompt_overrides["phase/<p>"] key; application overrides use
 * prompt_overrides["application/<app_id>"].
 */
import { useQuery } from '@tanstack/react-query'
import CodeMirror from '@uiw/react-codemirror'

import type { components } from '@apex/api-client'
import type { PhaseName } from '@apex/pipeline-events'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys, STALE_TIMES } from '@/api/queryKeys'
import { useOptionalConsumer } from '@/auth/AuthProvider'

import type { StepProps } from '../NewRunWizard'
import { focusedPromptPhase, isRecord } from '../wizardState'

type PromptSummary = components['schemas']['PromptSummary']
type PromptDetail = components['schemas']['PromptDetail']

const PHASE_NAMESPACE = 'phase'
const APPLICATION_NAMESPACE = 'application'

async function fetchPromptList(namespace: string): Promise<PromptSummary[]> {
  const { data, error, response } = await getApexClient().GET('/v1/prompts', {
    params: { query: { namespace } },
  })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Prompt list failed (${response.status})`),
      error,
    )
  }
  return data
}

async function fetchPromptDetail(promptId: string): Promise<PromptDetail> {
  const { data, error, response } = await getApexClient().GET('/v1/prompts/{prompt_id}', {
    params: { path: { prompt_id: promptId } },
  })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Prompt load failed (${response.status})`),
      error,
    )
  }
  return data
}

interface CatalogPrompt {
  content: string
  version: number | null
}

/** Catalog (content, active version) for one phase prompt key, or null when absent. */
function useCatalogPrompt(
  summaries: PromptSummary[] | undefined,
  key: string | null,
  enabled = true,
): { prompt: CatalogPrompt | null; loading: boolean; error: Error | null } {
  const summary = enabled && key ? summaries?.find((entry) => entry.key === key) : undefined
  const detail = useQuery({
    queryKey: queryKeys.prompts.byId(summary?.id ?? 'missing'),
    queryFn: () => fetchPromptDetail(summary?.id ?? ''),
    enabled: enabled && Boolean(summary),
    staleTime: STALE_TIMES.prompts,
  })
  if (!enabled || !key) return { prompt: null, loading: false, error: null }
  if (!summary) return { prompt: null, loading: summaries === undefined, error: null }
  if (detail.isError) return { prompt: null, loading: false, error: detail.error }
  if (!detail.data) return { prompt: null, loading: detail.isLoading, error: null }
  return {
    prompt: {
      content: detail.data.content ?? '',
      version: detail.data.active_version?.version ?? null,
    },
    loading: false,
    error: null,
  }
}

function PromptBlock({
  label,
  prompt,
  loading,
  error,
  override,
  overrideLabel,
  editorLabel,
  emptyText,
  emptyChipLabel = 'built-in default',
  testId,
  onOverride,
  onRevert,
  onEdit,
}: {
  label: string
  prompt: CatalogPrompt | null
  loading: boolean
  error?: Error | null
  override: { content: string } | undefined
  overrideLabel: string
  editorLabel: string
  emptyText: string
  emptyChipLabel?: string
  testId?: string
  onOverride: (seed: string) => void
  onRevert: () => void
  onEdit: (content: string) => void
}) {
  return (
    <div className="glass-panel wizard-prompt-block" data-testid={testId}>
      <div className="wizard-row">
        <span className="wizard-label">{label}</span>
        {override ? (
          <span className="topbar-meta-chip warning" data-testid={`override-chip-${overrideLabel}`}>
            run override
          </span>
        ) : prompt && prompt.version !== null ? (
          <span className="topbar-meta-chip accent">catalog@v{prompt.version}</span>
        ) : (
          !loading && <span className="topbar-meta-chip">{emptyChipLabel}</span>
        )}
        {override && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={onRevert}>
            Revert to catalog
          </button>
        )}
      </div>

      {override ? (
        <div className="code-viewer editable">
          <CodeMirror
            value={override.content}
            editable
            aria-label={editorLabel}
            basicSetup={{
              lineNumbers: true,
              foldGutter: false,
              highlightActiveLine: true,
              highlightActiveLineGutter: false,
            }}
            onChange={(next: string) => onEdit(next)}
          />
        </div>
      ) : error ? (
        <p className="wizard-caption wizard-caption--danger" role="alert">
          Catalog prompt could not be loaded. Retry or use an explicit override.
        </p>
      ) : loading ? (
        <p className="wizard-caption">Loading…</p>
      ) : prompt ? (
        <div className="code-viewer">
          <CodeMirror
            value={prompt.content}
            readOnly
            editable={false}
            basicSetup={{
              lineNumbers: false,
              foldGutter: false,
              highlightActiveLine: false,
              highlightActiveLineGutter: false,
            }}
          />
        </div>
      ) : (
        <p className="wizard-caption">{emptyText}</p>
      )}

      {!override && !loading && (
        <button
          type="button"
          className="btn btn-ghost btn-sm wizard-prompt-override"
          onClick={() => onOverride(prompt?.content ?? '')}
        >
          Override for this run
        </button>
      )}
    </div>
  )
}

export function PromptsStep({ draft, onChange }: StepProps) {
  const consumer = useOptionalConsumer()
  const phase = focusedPromptPhase(draft.config)
  const appId = draft.scope.app_id
  const phaseList = useQuery({
    queryKey: queryKeys.prompts.listNamespace(PHASE_NAMESPACE),
    queryFn: () => fetchPromptList(PHASE_NAMESPACE),
    staleTime: STALE_TIMES.prompts,
  })
  const applicationList = useQuery({
    queryKey: queryKeys.prompts.listNamespace(APPLICATION_NAMESPACE),
    queryFn: () => fetchPromptList(APPLICATION_NAMESPACE),
    // Application namespace is intentionally unavailable to scoped
    // identities; avoid a guaranteed 403 and explain that limitation below.
    enabled: Boolean(appId) && (consumer === undefined || (consumer != null && consumer.scopes.length === 0)),
    staleTime: STALE_TIMES.prompts,
  })

  const system = useCatalogPrompt(phaseList.data, phase ? `${phase}/system` : null)
  const application = useCatalogPrompt(
    applicationList.data,
    appId,
    Boolean(appId) && (consumer === undefined || (consumer !== null && consumer.scopes.length === 0)),
  )

  function systemOverrideKey(phaseName: PhaseName): string {
    return `${PHASE_NAMESPACE}/${phaseName}`
  }

  function applicationOverrideKey(applicationId: string): string {
    return `${APPLICATION_NAMESPACE}/${applicationId}`
  }

  const inheritedOverrides =
    draft.config.golden_configurable && isRecord(draft.config.golden_configurable['prompt_overrides'])
      ? draft.config.golden_configurable['prompt_overrides']
      : {}
  const removedOverrides = new Set(draft.prompt_override_removals)
  function effectiveOverride(key: string): { content: string } | undefined {
    if (draft.prompt_overrides[key]) return draft.prompt_overrides[key]
    if (removedOverrides.has(key)) return undefined
    const inherited = inheritedOverrides[key]
    return isRecord(inherited)
      ? { content: typeof inherited['content'] === 'string' ? inherited['content'] : '' }
      : undefined
  }
  const systemOverride = phase ? effectiveOverride(systemOverrideKey(phase)) : undefined
  const applicationOverride = appId ? effectiveOverride(applicationOverrideKey(appId)) : undefined

  return (
    <section className="wizard-step" aria-label="Prompts">
      <p className="wizard-step-hint">
        Preview the focused phase system prompt and the selected application's requirements prompt.
      </p>
      {phaseList.isError && (
        <p className="wizard-caption wizard-caption--danger" role="alert">
          Phase prompt catalog failed to load — built-in defaults still apply at run time.
        </p>
      )}
      {applicationList.isError && (
        <p className="wizard-caption wizard-caption--danger" role="alert">
          Application prompt catalog failed to load.
        </p>
      )}
      {appId && consumer != null && consumer.scopes.length > 0 && (
        <p className="wizard-caption">Application prompt catalog is available only to unscoped operators.</p>
      )}

      {phase === null ? (
        <p className="wizard-caption wizard-caption--danger">Select at least one phase first.</p>
      ) : (
        <div className="wizard-prompt-panel">
          <div className="wizard-row">
            <span className="topbar-meta-chip accent" data-testid="prompt-focused-phase">
              {phase.replaceAll('_', ' ')}
            </span>
            {appId && (
              <span className="topbar-meta-chip" data-testid="prompt-selected-application">
                {appId}
              </span>
            )}
          </div>

          <PromptBlock
            label="System prompt"
            prompt={system.prompt}
            loading={system.loading}
            error={system.error}
            override={systemOverride}
            overrideLabel={phase}
            editorLabel={`${phase} system prompt override`}
            emptyText="Not in the catalog — the built-in phase template runs."
            testId="system-prompt-block"
            onOverride={(seed) =>
              onChange((prev) => ({
                ...prev,
                prompt_overrides: {
                  ...prev.prompt_overrides,
                  [systemOverrideKey(phase)]: { content: seed },
                },
                prompt_override_removals: prev.prompt_override_removals.filter((key) => key !== systemOverrideKey(phase)),
              }))
            }
            onRevert={() =>
              onChange((prev) => {
                const next = { ...prev.prompt_overrides }
                delete next[systemOverrideKey(phase)]
                const inherited =
                  prev.config.golden_configurable && isRecord(prev.config.golden_configurable['prompt_overrides'])
                    ? prev.config.golden_configurable['prompt_overrides']
                    : {}
                const removals = Object.prototype.hasOwnProperty.call(inherited, systemOverrideKey(phase))
                  ? [...new Set([...prev.prompt_override_removals, systemOverrideKey(phase)])]
                  : prev.prompt_override_removals.filter((key) => key !== systemOverrideKey(phase))
                return { ...prev, prompt_overrides: next, prompt_override_removals: removals }
              })
            }
            onEdit={(content) =>
              onChange((prev) => ({
                ...prev,
                prompt_overrides: {
                  ...prev.prompt_overrides,
                  [systemOverrideKey(phase)]: { content },
                },
                prompt_override_removals: prev.prompt_override_removals.filter((key) => key !== systemOverrideKey(phase)),
              }))
            }
          />

          {appId ? (
            <PromptBlock
              label="Application prompt"
              prompt={application.prompt}
              loading={application.loading}
              error={application.error}
              override={applicationOverride}
              overrideLabel={appId}
              editorLabel={`${appId} application prompt override`}
              emptyText="No application prompt exists for this app yet."
              emptyChipLabel="empty"
              testId="application-prompt-block"
              onOverride={(seed) =>
                onChange((prev) => ({
                  ...prev,
                  prompt_overrides: {
                    ...prev.prompt_overrides,
                    [applicationOverrideKey(appId)]: { content: seed },
                  },
                  prompt_override_removals: prev.prompt_override_removals.filter((key) => key !== applicationOverrideKey(appId)),
                }))
              }
              onRevert={() =>
                onChange((prev) => {
                  const next = { ...prev.prompt_overrides }
                  delete next[applicationOverrideKey(appId)]
                  const inherited =
                    prev.config.golden_configurable && isRecord(prev.config.golden_configurable['prompt_overrides'])
                      ? prev.config.golden_configurable['prompt_overrides']
                      : {}
                  const key = applicationOverrideKey(appId)
                  const removals = Object.prototype.hasOwnProperty.call(inherited, key)
                    ? [...new Set([...prev.prompt_override_removals, key])]
                    : prev.prompt_override_removals.filter((entry) => entry !== key)
                  return { ...prev, prompt_overrides: next, prompt_override_removals: removals }
                })
              }
              onEdit={(content) =>
                onChange((prev) => ({
                  ...prev,
                  prompt_overrides: {
                    ...prev.prompt_overrides,
                    [applicationOverrideKey(appId)]: { content },
                  },
                  prompt_override_removals: prev.prompt_override_removals.filter((key) => key !== applicationOverrideKey(appId)),
                }))
              }
            />
          ) : (
            <div className="glass-panel wizard-prompt-block">
              <div className="wizard-row">
                <span className="wizard-label">Application prompt</span>
              </div>
              <p className="wizard-caption">Select an application in Scope to load its requirements prompt.</p>
            </div>
          )}
        </div>
      )}
    </section>
  )
}
