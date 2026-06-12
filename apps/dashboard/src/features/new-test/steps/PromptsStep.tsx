/**
 * Step 5 — Prompts: an accordion per included phase previewing the catalog's
 * active phase/<p>/system + phase/<p>/user content (provenance chip
 * "catalog@vN"); [Override for this run] swaps the system prompt to an
 * editable editor writing prompt_overrides["phase/<p>"] = {content} ("run
 * override" chip + revert). Per the backend prompt resolver, a run override
 * replaces the SYSTEM prompt only — the user prompt always comes from
 * catalog/builtin (src/apex/services/prompts.py).
 */
import { useQuery } from '@tanstack/react-query'
import CodeMirror from '@uiw/react-codemirror'

import type { components } from '@apex/api-client'
import type { PhaseName } from '@apex/pipeline-events'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys, STALE_TIMES } from '@/api/queryKeys'

import type { StepProps } from '../NewRunWizard'
import { selectedPhases } from '../wizardState'

type PromptSummary = components['schemas']['PromptSummary']
type PromptDetail = components['schemas']['PromptDetail']

const PHASE_NAMESPACE = 'phase'

async function fetchPhasePromptList(): Promise<PromptSummary[]> {
  const { data, error, response } = await getApexClient().GET('/v1/prompts', {
    params: { query: { namespace: PHASE_NAMESPACE } },
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
  key: string,
): { prompt: CatalogPrompt | null; loading: boolean } {
  const summary = summaries?.find((entry) => entry.key === key)
  const detail = useQuery({
    queryKey: queryKeys.prompts.byId(summary?.id ?? 'missing'),
    queryFn: () => fetchPromptDetail(summary?.id ?? ''),
    enabled: Boolean(summary),
    staleTime: STALE_TIMES.prompts,
  })
  if (!summary) return { prompt: null, loading: summaries === undefined }
  if (!detail.data) return { prompt: null, loading: detail.isLoading }
  return {
    prompt: {
      content: detail.data.content ?? '',
      version: detail.data.active_version?.version ?? null,
    },
    loading: false,
  }
}

function ReadOnlyPrompt({ label, prompt, loading }: { label: string; prompt: CatalogPrompt | null; loading: boolean }) {
  return (
    <div className="wizard-prompt-block">
      <div className="wizard-row">
        <span className="wizard-label">{label}</span>
        {prompt && prompt.version !== null ? (
          <span className="topbar-meta-chip accent">catalog@v{prompt.version}</span>
        ) : (
          !loading && <span className="topbar-meta-chip">built-in default</span>
        )}
      </div>
      {loading ? (
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
        <p className="wizard-caption">Not in the catalog — the built-in template runs.</p>
      )}
    </div>
  )
}

function PhasePromptPanel({
  phase,
  summaries,
  override,
  onOverride,
  onRevert,
  onEdit,
}: {
  phase: PhaseName
  summaries: PromptSummary[] | undefined
  override: { content: string } | undefined
  onOverride: (seed: string) => void
  onRevert: () => void
  onEdit: (content: string) => void
}) {
  const system = useCatalogPrompt(summaries, `${phase}/system`)
  const user = useCatalogPrompt(summaries, `${phase}/user`)

  return (
    <div className="wizard-prompt-panel">
      {override ? (
        <div className="wizard-prompt-block">
          <div className="wizard-row">
            <span className="wizard-label">System prompt</span>
            <span className="topbar-meta-chip warning" data-testid={`override-chip-${phase}`}>
              run override
            </span>
            <button type="button" className="btn btn-ghost btn-sm" onClick={onRevert}>
              Revert to catalog
            </button>
          </div>
          <div className="code-viewer editable">
            <CodeMirror
              value={override.content}
              editable
              aria-label={`${phase} system prompt override`}
              basicSetup={{
                lineNumbers: true,
                foldGutter: false,
                highlightActiveLine: true,
                highlightActiveLineGutter: false,
              }}
              onChange={(next: string) => onEdit(next)}
            />
          </div>
        </div>
      ) : (
        <>
          <ReadOnlyPrompt label="System prompt" prompt={system.prompt} loading={system.loading} />
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => onOverride(system.prompt?.content ?? '')}
          >
            Override for this run
          </button>
        </>
      )}
      <ReadOnlyPrompt label="User prompt" prompt={user.prompt} loading={user.loading} />
      <p className="wizard-caption">
        Overrides replace the system prompt for this run only; the user prompt stays on its
        catalog/builtin template.
      </p>
    </div>
  )
}

export function PromptsStep({ draft, onChange }: StepProps) {
  const phases = selectedPhases(draft.config)
  const list = useQuery({
    queryKey: queryKeys.prompts.listNamespace(PHASE_NAMESPACE),
    queryFn: fetchPhasePromptList,
    staleTime: STALE_TIMES.prompts,
  })

  function overrideKey(phase: PhaseName): string {
    return `${PHASE_NAMESPACE}/${phase}`
  }

  return (
    <section className="wizard-step" aria-label="Prompts">
      <p className="wizard-step-hint">
        Preview the catalog prompts each included phase will run with, and override per run where
        needed.
      </p>
      {list.isError && (
        <p className="wizard-caption wizard-caption--danger" role="alert">
          Prompt catalog failed to load — built-in defaults still apply at run time.
        </p>
      )}
      {phases.map((phase) => {
        const override = draft.prompt_overrides[overrideKey(phase)]
        return (
          <details key={phase} className="glass-panel wizard-accordion">
            <summary className="wizard-accordion-summary">
              <span>{phase.replaceAll('_', ' ')}</span>
              {override && (
                <span className="topbar-meta-chip warning" data-testid={`summary-override-${phase}`}>
                  run override
                </span>
              )}
            </summary>
            <PhasePromptPanel
              phase={phase}
              summaries={list.data}
              override={override}
              onOverride={(seed) =>
                onChange((prev) => ({
                  ...prev,
                  prompt_overrides: {
                    ...prev.prompt_overrides,
                    [overrideKey(phase)]: { content: seed },
                  },
                }))
              }
              onRevert={() =>
                onChange((prev) => {
                  const next = { ...prev.prompt_overrides }
                  delete next[overrideKey(phase)]
                  return { ...prev, prompt_overrides: next }
                })
              }
              onEdit={(content) =>
                onChange((prev) => ({
                  ...prev,
                  prompt_overrides: {
                    ...prev.prompt_overrides,
                    [overrideKey(phase)]: { content },
                  },
                }))
              }
            />
          </details>
        )
      })}
    </section>
  )
}
