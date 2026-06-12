import { describe, expect, it } from 'vitest'

import {
  assessPlan,
  lastPlanSelection,
  PHASE_ORDER,
  PHASE_PREREQUISITES,
  runFromHereSelection,
  STALE_AFTER_MS,
  type PreflightPhaseResults,
} from '../preflight'

const NOW = Date.parse('2026-06-12T12:00:00Z')
const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString()

/** Thread results: fresh success, old success, and a failure. */
const RESULTS: PreflightPhaseResults = {
  story_analysis: { status: 'succeeded', attempt: 2, ended_at: iso(60_000) },
  test_planning: { status: 'succeeded', attempt: 1, ended_at: iso(120_000) },
  script_scenario: { status: 'succeeded', attempt: 1, ended_at: iso(4 * 24 * 3600 * 1000) },
  execution: { status: 'failed', attempt: 1, ended_at: iso(30_000) },
}

describe('preflight mirrors (apex.domain.pipeline)', () => {
  it('pins the canonical order and prerequisite edges', () => {
    expect(PHASE_ORDER).toEqual([
      'story_analysis',
      'test_planning',
      'env_triage',
      'script_scenario',
      'execution',
      'reporting',
      'postmortem',
    ])
    expect(PHASE_PREREQUISITES).toEqual({
      story_analysis: [],
      test_planning: ['story_analysis'],
      env_triage: [],
      script_scenario: ['test_planning'],
      execution: ['script_scenario'],
      reporting: ['execution'],
      postmortem: ['reporting'],
    })
  })
})

describe('assessPlan', () => {
  it.each([
    {
      name: 'no prerequisites -> ok',
      selected: ['story_analysis', 'env_triage'] as const,
      phase: 'env_triage',
      level: 'ok',
    },
    {
      name: 'prerequisite earlier in the plan -> ok (even when the thread result failed)',
      selected: ['execution', 'script_scenario', 'test_planning', 'story_analysis'] as const,
      phase: 'execution',
      level: 'ok',
    },
    {
      name: 'prerequisite succeeded on thread -> reuse',
      selected: ['test_planning'] as const,
      phase: 'test_planning',
      level: 'reuse',
    },
    {
      name: 'prerequisite succeeded >3d ago -> stale',
      selected: ['execution'] as const,
      phase: 'execution',
      level: 'stale',
    },
    {
      name: 'prerequisite failed on thread -> blocked',
      selected: ['reporting'] as const,
      phase: 'reporting',
      level: 'blocked',
    },
    {
      name: 'prerequisite never ran -> blocked',
      selected: ['postmortem'] as const,
      phase: 'postmortem',
      level: 'blocked',
    },
  ])('$name', ({ selected, phase, level }) => {
    const { rows } = assessPlan([...selected], RESULTS, NOW)
    const row = rows.find((r) => r.phase === phase)
    expect(row?.level).toBe(level)
  })

  it('reuse rows carry attempt + age and the "will reuse" copy', () => {
    const { rows, hasBlockers } = assessPlan(['test_planning'], RESULTS, NOW)
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({
      phase: 'test_planning',
      level: 'reuse',
      prereq: 'story_analysis',
      attempt: 2,
      age: '1m ago',
    })
    expect(rows[0]?.message).toBe('Will reuse Story Analysis artifacts (attempt 2, 1m ago).')
    expect(hasBlockers).toBe(false)
  })

  it('stale rows warn about drift but do NOT count as blockers', () => {
    const { rows, hasBlockers } = assessPlan(['execution'], RESULTS, NOW)
    expect(rows[0]?.level).toBe('stale')
    expect(rows[0]?.message).toContain('environment may have drifted')
    expect(hasBlockers).toBe(false)
  })

  it('the stale boundary is exactly STALE_AFTER_MS', () => {
    const results: PreflightPhaseResults = {
      script_scenario: { status: 'succeeded', attempt: 1, ended_at: iso(STALE_AFTER_MS) },
    }
    expect(assessPlan(['execution'], results, NOW).rows[0]?.level).toBe('reuse')
    const older: PreflightPhaseResults = {
      script_scenario: { status: 'succeeded', attempt: 1, ended_at: iso(STALE_AFTER_MS + 1) },
    }
    expect(assessPlan(['execution'], older, NOW).rows[0]?.level).toBe('stale')
  })

  it('blocked rows name the missing prerequisite and set hasBlockers', () => {
    const { rows, hasBlockers } = assessPlan(['reporting'], RESULTS, NOW)
    expect(rows[0]?.message).toBe('Include Execution or it will fail at plan resolution.')
    expect(hasBlockers).toBe(true)
  })

  it('a succeeded prerequisite without ended_at reuses with unknown age (never stale)', () => {
    const results: PreflightPhaseResults = {
      story_analysis: { status: 'succeeded', attempt: 1 },
    }
    const { rows } = assessPlan(['test_planning'], results, NOW)
    expect(rows[0]).toMatchObject({ level: 'reuse', age: undefined })
    expect(rows[0]?.message).toContain('age unknown')
  })

  it('returns rows in canonical order regardless of selection order', () => {
    const { rows } = assessPlan(['postmortem', 'story_analysis', 'execution'], RESULTS, NOW)
    expect(rows.map((r) => r.phase)).toEqual(['story_analysis', 'execution', 'postmortem'])
  })

  it('handles missing phase_results (fresh thread): downstream phases are blocked', () => {
    const { rows, hasBlockers } = assessPlan(['story_analysis', 'reporting'], undefined, NOW)
    expect(rows.map((r) => [r.phase, r.level])).toEqual([
      ['story_analysis', 'ok'],
      ['reporting', 'blocked'],
    ])
    expect(hasBlockers).toBe(true)
  })
})

describe('selection helpers', () => {
  it('runFromHereSelection = the phase + downstream phases of the LAST plan', () => {
    const plan = ['story_analysis', 'test_planning', 'script_scenario', 'execution']
    // env_triage was not in the last plan -> excluded from the tail.
    expect(runFromHereSelection('test_planning', plan)).toEqual([
      'test_planning',
      'script_scenario',
      'execution',
    ])
    // The anchor phase is always included, even when absent from the plan.
    expect(runFromHereSelection('env_triage', ['story_analysis'])).toEqual(['env_triage'])
    expect(runFromHereSelection('postmortem', undefined)).toEqual(['postmortem'])
  })

  it('lastPlanSelection filters junk and canonicalizes order', () => {
    expect(lastPlanSelection(['execution', 'story_analysis', 'bogus'])).toEqual([
      'story_analysis',
      'execution',
    ])
    expect(lastPlanSelection(undefined)).toEqual([])
  })
})
