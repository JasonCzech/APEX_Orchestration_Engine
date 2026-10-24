import type { Application } from '@/api/hooks/useEnvironments'

export interface TestParticipant {
  id: string
  project_id: string
  application_id: string
  environment_id: string
  layer: 'frontend' | 'middle' | 'backend'
}
export interface TestScopeV1 {
  schema_version: 1
  participants: TestParticipant[]
  primary_participant_id: string
  traffic_entry_participant_id: string | null
}
export const emptyTestScope = (): TestScopeV1 => ({ schema_version: 1, participants: [], primary_participant_id: '', traffic_entry_participant_id: null })
export function testScopeIssues(scope: TestScopeV1 | undefined, max = 32): string[] {
  if (!scope) return ['Complete the system topology: select layers, one backend, and a primary system.']
  const issues: string[] = []
  if (scope.participants.length > max) issues.push(`Select at most ${max} participants.`)
  if (scope.participants.filter(p => p.layer === 'frontend').length > 1) issues.push('Only one frontend is allowed.')
  if (scope.participants.filter(p => p.layer === 'backend').length !== 1) issues.push('Exactly one backend is required.')
  if (scope.participants.some(p => !p.project_id || !p.application_id || !p.environment_id)) issues.push('Choose an application and environment for every system, and a project for the primary system.')
  if (new Set(scope.participants.map(p => p.id)).size !== scope.participants.length || new Set(scope.participants.map(p => `${p.application_id}/${p.environment_id}`)).size !== scope.participants.length) issues.push('Each application/environment pair must be unique.')
  if (!scope.participants.some(p => p.id === scope.primary_participant_id)) issues.push('Select the primary system under test.')
  if (scope.traffic_entry_participant_id && !scope.participants.some(p => p.id === scope.traffic_entry_participant_id)) issues.push('Select a valid traffic entry.')
  return issues
}
export function parseTestScope(value: unknown): TestScopeV1 | undefined {
  if (!value || typeof value !== 'object') return undefined
  const raw = value as Record<string, unknown>
  if (raw.schema_version !== 1 || !Array.isArray(raw.participants)) return undefined
  const participants = raw.participants.filter((p): p is TestParticipant => Boolean(p) && typeof p === 'object' && ['id', 'project_id', 'application_id', 'environment_id'].every(k => typeof p[k] === 'string') && ['frontend', 'middle', 'backend'].includes(p.layer))
  return { schema_version: 1, participants, primary_participant_id: typeof raw.primary_participant_id === 'string' ? raw.primary_participant_id : '', traffic_entry_participant_id: typeof raw.traffic_entry_participant_id === 'string' ? raw.traffic_entry_participant_id : null }
}

/** Projects through which this consumer may act on `app` (every linked project when unscoped). */
export function accessibleProjects(app: Application | undefined, consumer: ConsumerLike): string[] {
  if (!app) return []
  if (!consumer || consumer.scopes.length === 0) return app.project_ids
  return app.project_ids.filter(project => consumer.scopes.some(scope => scope.project_id === project && (!scope.app_id || scope.app_id === app.id)))
}

/** Projects this consumer can own a test in: those its scopes reach (every linked one when unscoped). */
export function reachableProjects(apps: readonly Application[], consumer: ConsumerLike): string[] {
  const scoped = consumer?.scopes.map(scope => scope.project_id) ?? []
  return [...new Set([...apps.flatMap(app => accessibleProjects(app, consumer)), ...scoped])]
}

/**
 * A non-primary system's project: the owning project when the application is
 * linked to it, otherwise the single project the consumer can reach it through.
 * Several candidates leave the choice to the person ("via project").
 */
export function derivedProject(app: Application | undefined, owningProject: string, consumer: ConsumerLike): string {
  const candidates = accessibleProjects(app, consumer)
  if (owningProject && candidates.includes(owningProject)) return owningProject
  return candidates.length === 1 ? candidates[0]! : ''
}

export type ConsumerLike = { scopes: { project_id: string; app_id?: string | null }[] } | null | undefined
