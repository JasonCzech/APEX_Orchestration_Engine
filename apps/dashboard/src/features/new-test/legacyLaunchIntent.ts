/** Frozen pre-ownership intent shape, used only to quarantine ambiguous launches.
 * Never use this projection as a launch request or replay its older semantics.
 */
import type { PhaseName } from '@apex/pipeline-events'
import { gateMatrixOf, selectedPhases, type WizardDraft } from './wizardState'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function legacyConfigurable(draft: WizardDraft): Record<string, unknown> {
  const { scope, config } = draft
  const overrides = Object.entries(draft.legacy_prompt_overrides ?? draft.prompt_overrides)
  const inherited = { ...(config.golden_configurable ?? {}) }
  for (const key of ['phase_execution', 'analysis', 'test_scope', 'test_scope_snapshot', 'prompt_selections', 'prompt_manifest', 'rerun_phases']) delete inherited[key]
  const inheritedOverrides = isRecord(inherited['prompt_overrides'])
    ? inherited['prompt_overrides']
    : {}
  const removedOverrides = new Set(draft.legacy_prompt_override_removals ?? draft.prompt_override_removals)
  const effectiveOverrides: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(inheritedOverrides)) {
    if (removedOverrides.has(key)) continue
    effectiveOverrides[key] = value
  }
  for (const [key, value] of overrides) effectiveOverrides[key] = { content: value.content }
  const includedPhases = new Set(selectedPhases(config))
  for (const key of Object.keys(effectiveOverrides)) {
    if ((key.startsWith('phase/') && !includedPhases.has(key.split('/')[1] as PhaseName)) ||
      (key.startsWith('application/') && key !== `application/${scope.app_id}`)) {
      delete effectiveOverrides[key]
    }
  }
  const configurable: Record<string, unknown> = {
    ...inherited,
    project_id: scope.project_id.trim(),
    engine: config.engine,
    gates: gateMatrixOf(config),
    ...(Object.keys(effectiveOverrides).length > 0
      ? {
          prompt_overrides: effectiveOverrides,
        }
      : {}),
  }
  const selectedWorkTrackingConnections = new Set(
    draft.work_items
      .map((item) => item.connection_id)
      .filter((connectionId): connectionId is string => Boolean(connectionId)),
  )
  if (selectedWorkTrackingConnections.size === 1) {
    const inheritedConnections = isRecord(inherited['connections'])
      ? inherited['connections']
      : {}
    configurable['connections'] = {
      ...inheritedConnections,
      work_tracking: [...selectedWorkTrackingConnections][0],
    }
  }
  if (Object.keys(effectiveOverrides).length === 0) delete configurable['prompt_overrides']

  // The compact scope/phase controls are authoritative even when they clear a
  // value inherited from an assistant bundle.
  if (scope.app_id) configurable['app_id'] = scope.app_id
  else delete configurable['app_id']
  if (scope.environment_id) configurable['environment_id'] = scope.environment_id
  else delete configurable['environment_id']
  delete configurable['environment_target']
  delete configurable['environment_target_version']
  delete configurable['start_phase']
  delete configurable['stop_after']
  // The wizard's visible plan is authoritative, including all seven phases.
  // Sparse assistant-inherited API requests outside the wizard remain distinct.
  if (config.phases === null) delete configurable['phases']
  else configurable['phases'] = selectedPhases(config)
  // This field was never consumed by the graph. Context is now resolved into
  // input.context_packets by the domain launch endpoint.
  delete configurable['pre_execution_context']
  return configurable
}

export function legacyLaunchIntent(draft: WizardDraft): unknown {
  return {
    assistant_id: draft.config.golden_config_id ?? 'pipeline',
    title: draft.title.trim(),
    request: draft.request.trim(),
    scope: {
      project_id: draft.scope.project_id.trim(),
      app_id: draft.scope.app_id,
      environment_id: draft.scope.environment_id,
    },
    configurable: legacyConfigurable(draft),
    document_ids: [...draft.document_ids],
    work_items: draft.work_items.map(item => ({ ...item })),
    context_summary_ids: [...draft.context_summary_ids],
  }
}
