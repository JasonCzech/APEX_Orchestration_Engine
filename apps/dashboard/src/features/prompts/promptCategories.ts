import type { PhaseName } from '@apex/pipeline-events'

export const PROMPT_CATEGORIES = ['system', 'phase', 'application', 'environment'] as const
export const PROMPT_LABELS: Record<string, string> = {
  system: 'System prompt', phase: 'Phase context', application: 'Application context', environment: 'Environment context',
}
export interface PromptSelection {
  prompt_id: string
  version_id: string
  override_content?: string
}
export interface DraftPromptSelections {
  schema_version: 1
  system: PromptSelection | null
  phases: Partial<Record<PhaseName, PromptSelection>>
  application: PromptSelection[]
  environment: PromptSelection[]
}
export const emptyPromptSelections = (): DraftPromptSelections => ({ schema_version: 1, system: null, phases: {}, application: [], environment: [] })

export function parsePromptSelections(value: unknown): DraftPromptSelections | undefined {
  if (!value || typeof value !== 'object') return undefined
  const raw = value as Record<string, unknown>
  if (raw.schema_version !== 1) return undefined
  const selection = (item: unknown): PromptSelection | null => {
    if (!item || typeof item !== 'object') return null
    const row = item as Record<string, unknown>
    return typeof row.prompt_id === 'string' && typeof row.version_id === 'string'
      ? { prompt_id: row.prompt_id, version_id: row.version_id, ...(typeof row.override_content === 'string' ? { override_content: row.override_content } : {}) } : null
  }
  const result = emptyPromptSelections()
  result.system = selection(raw.system)
  if (raw.phases && typeof raw.phases === 'object') {
    for (const [key, item] of Object.entries(raw.phases)) {
      const parsed = selection(item)
      if (parsed) result.phases[key as PhaseName] = parsed
    }
  }
  for (const category of ['application', 'environment'] as const) {
    result[category] = Array.isArray(raw[category]) ? raw[category].map(selection).filter((item): item is PromptSelection => item !== null) : []
  }
  return result
}
