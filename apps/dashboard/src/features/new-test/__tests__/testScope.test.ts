import { describe, expect, it } from 'vitest'
import { parseTestScope, testScopeIssues, type TestScopeV1 } from '../testScope'
import { buildLaunchPreview, emptyDraft, parseDraftPayload } from '../wizardState'

const topology = (): TestScopeV1 => ({
  schema_version: 1,
  participants: [
    { id: 'web', project_id: 'p1', application_id: 'web', environment_id: 'web-perf', layer: 'frontend' },
    { id: 'api', project_id: 'p2', application_id: 'api', environment_id: 'api-perf', layer: 'middle' },
    { id: 'queue', project_id: 'p3', application_id: 'queue', environment_id: 'queue-perf', layer: 'middle' },
    { id: 'db', project_id: 'p4', application_id: 'db', environment_id: 'db-perf', layer: 'backend' },
  ],
  primary_participant_id: 'api', traffic_entry_participant_id: 'web',
})

describe('end-to-end scope', () => {
  it('round trips all participants and independent primary/traffic selections', () => {
    const draft = emptyDraft()
    draft.scope = { project_id: 'p2', app_id: 'api', environment_id: 'web-perf', test_scope: topology() }
    const restored = parseDraftPayload(JSON.parse(JSON.stringify(draft)))
    expect(restored.scope).toEqual(draft.scope)
    expect(testScopeIssues(restored.scope.test_scope)).toEqual([])
    const preview = buildLaunchPreview(restored)
    expect(preview.configurable.test_scope).toEqual(topology())
    expect(preview.configurable.app_id).toBe('api')
    expect(preview.configurable.environment_id).toBe('web-perf')
  })
  it('requires explicit completion of historical setup without inventing a backend', () => {
    const draft = parseDraftPayload({ scope: { project_id: 'p1', app_id: 'old', environment_id: 'env' } })
    expect(draft.scope.app_id).toBe('old')
    expect(draft.scope.test_scope).toBeUndefined()
    expect(testScopeIssues(draft.scope.test_scope)).not.toEqual([])
  })
  it('rejects duplicate pairs, extra frontend, missing backend and dangling roles', () => {
    const value = topology()
    value.participants.push({ ...value.participants[0]!, id: 'duplicate' })
    expect(testScopeIssues(value)).toContain('Only one frontend is allowed.')
    expect(testScopeIssues(value)).toContain('Each application/environment pair must be unique.')
    value.participants = value.participants.filter(p => p.layer !== 'backend' && p.id !== 'api')
    expect(testScopeIssues(value)).toContain('Exactly one backend is required.')
    expect(testScopeIssues(value)).toContain('Select the primary system under test.')
    expect(testScopeIssues(topology(), 3)).toContain('Select at most 3 participants.')
  })
  it('does not silently change primary when a system disappears', () => {
    const value = topology()
    value.participants = value.participants.filter(p => p.id !== 'api')
    const restored = parseTestScope(value)!
    expect(restored.primary_participant_id).toBe('api')
    expect(testScopeIssues(restored)).toContain('Select the primary system under test.')
  })
})
