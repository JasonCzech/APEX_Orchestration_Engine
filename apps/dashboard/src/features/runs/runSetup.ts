import { PHASE_NAMES } from '@apex/pipeline-events'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { isRecord, parseDraftPayload, type WizardDraft } from '@/features/new-test/wizardState'

/** A copy keeps its source's name visibly distinct so two runs never share one
 * title: "Name" -> "Name (copy)" -> "Name (copy 2)". Fits the 500-char limit. */
export function copyTitle(title: string) {
  const trimmed = title.trim()
  const match = /^(.*) \(copy(?: (\d+))?\)$/.exec(trimmed)
  const base = match ? match[1]! : trimmed
  const suffix = match ? ` (copy ${Number(match[2] ?? 1) + 1})` : ' (copy)'
  return Array.from(base).slice(0, 500 - suffix.length).join('') + suffix
}

/** A test's frozen setup as a wizard draft (launch revalidates every reference). */
export async function loadRunSetupDraft(threadId: string, signal?: AbortSignal): Promise<WizardDraft> {
  const { data, error: failure, response } = await getApexClient().GET('/v1/pipelines/{thread_id}/setup', { params: { path: { thread_id: threadId } }, signal })
  if (!response.ok || !data) throw new ApiError(response.status, errorMessageOf(failure, 'Could not copy this test setup.'))
  const cfg = data.configurable
  const owners = isRecord(cfg.phase_execution) && isRecord(cfg.phase_execution.owners) ? cfg.phase_execution.owners : null
  return parseDraftPayload({
    title: copyTitle(data.title), request: data.request,
    scope: { test_scope: data.test_scope ?? cfg.test_scope, project_id: data.project_id, app_id: data.app_id, environment_id: data.environment_id },
    config: { engine: cfg.engine, phases: owners ? PHASE_NAMES.filter(phase => owners[phase] === 'langgraph') : cfg.phases, ...(owners ? { schema_version: 2 } : {}), analysis: cfg.analysis, gates_mode: 'custom', gates_custom: cfg.gates, golden_configurable: cfg },
    prompt_selections: cfg.prompt_selections, prompt_overrides: cfg.prompt_overrides,
  })
}
