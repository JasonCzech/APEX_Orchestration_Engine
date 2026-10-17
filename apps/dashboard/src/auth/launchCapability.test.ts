import { describe, expect, it } from 'vitest'
import type { ConsumerInfo } from '@/api/apexClient'
import { canLaunchPipelines, canMutateAudience, pipelineLaunchRefusal } from './RequireRole'

const consumer = (role: ConsumerInfo['role'], scopes: ConsumerInfo['scopes'] = []): ConsumerInfo => ({ name: 'Reviewer', role, scopes })

describe('pipeline launch capabilities', () => {
  it('distinguishes missing role from missing scope before authoring begins', () => {
    expect(canLaunchPipelines(consumer('viewer', [{ project_id: 'p', app_id: null }]))).toBe(false)
    expect(pipelineLaunchRefusal(consumer('viewer'))).toContain("role 'operator'")
    expect(canLaunchPipelines(consumer('operator'))).toBe(false)
    expect(pipelineLaunchRefusal(consumer('operator'))).toContain('scope is not authorized')
  })
  it('allows scoped operators and admins only in their exact audience', () => {
    const operator = consumer('operator', [{ project_id: 'p', app_id: 'a' }])
    expect(canLaunchPipelines(operator)).toBe(true)
    expect(canMutateAudience(operator, 'p', 'a')).toBe(true)
    expect(canMutateAudience(operator, 'p', 'b')).toBe(false)
    expect(canMutateAudience(operator, 'p', null)).toBe(false)
    expect(canLaunchPipelines(consumer('admin'))).toBe(true)
  })
})
