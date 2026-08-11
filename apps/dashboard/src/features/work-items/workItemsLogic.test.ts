import { describe, expect, it } from 'vitest'

import type { WorkItem } from '@/api/hooks/useWorkTracking'

import { queryDialect, statusTone, workItemFacts, workItemPacketSummary } from './workItemsLogic'

function item(overrides: Partial<WorkItem> = {}): WorkItem {
  return {
    key: 'PHX-101',
    title: 'Checkout retries drop payments',
    kind: 'story',
    status: 'open',
    description: '',
    url: null,
    ...overrides,
  }
}

describe('statusTone', () => {
  it.each([
    ['open', 'accent'],
    ['In Progress', 'info'],
    ['Closed', 'success'],
    ['blocked', 'danger'],
    ['ready_for_test', 'neutral'],
  ])('keeps tracker-neutral statuses working: %s', (status, tone) => {
    expect(statusTone(status)).toBe(tone)
  })

  it.each([
    ['Pending Approval', 'warning'],
    ['Ready', 'accent'],
    ['Building', 'info'],
    ['Testing', 'info'],
    ['Accepted', 'success'],
  ])('tones the Jira Align workflow states: %s', (status, tone) => {
    expect(statusTone(status)).toBe(tone)
  })
})

describe('queryDialect', () => {
  it('names the dialect each known provider speaks', () => {
    expect(queryDialect('jira')?.name).toBe('JQL')
    expect(queryDialect('ado')?.name).toBe('WIQL')
    expect(queryDialect('jira_align')?.name).toBe('OData $filter')
  })

  it('stays generic for a provider the dashboard does not know', () => {
    expect(queryDialect('rally')).toBeNull()
    expect(queryDialect('')).toBeNull()
  })
})

describe('workItemFacts', () => {
  it('returns nothing when the tracker exposes no triage context', () => {
    expect(workItemFacts(item())).toEqual([])
  })

  it('orders the facts a triager reads first and marks related keys', () => {
    const facts = workItemFacts(
      item({
        priority: 'P1',
        severity: 'Sev-2',
        sprint: '24.3',
        assignee: 'Jamie Rivera',
        parent_key: 'PHX-100',
        linked_keys: ['PHX-300'],
        labels: ['payments', 'checkout'],
      }),
    )

    expect(facts.map((fact) => [fact.label, fact.value])).toEqual([
      ['Priority', 'P1'],
      ['Severity', 'Sev-2'],
      ['Sprint', '24.3'],
      ['Assignee', 'Jamie Rivera'],
      ['Parent', 'PHX-100'],
      ['Linked', 'PHX-300'],
      ['Labels', 'payments, checkout'],
    ])
    expect(facts.find((fact) => fact.label === 'Parent')?.relatedKey).toBe('PHX-100')
    expect(facts.find((fact) => fact.label === 'Labels')?.relatedKey).toBeUndefined()
  })

  it('skips blank values rather than rendering an empty row', () => {
    expect(workItemFacts(item({ priority: '   ', labels: ['', ' '] }))).toEqual([])
  })
})

describe('workItemPacketSummary', () => {
  it('falls back to kind and status when there is nothing else to say', () => {
    expect(workItemPacketSummary(item())).toBe('story · open')
  })

  it('carries the triage context story analysis would otherwise never see', () => {
    const summary = workItemPacketSummary(
      item({ kind: 'bug', status: 'open', priority: 'P1', assignee: 'Alex Chen' }),
    )
    expect(summary).toBe('bug · open · priority P1 · assignee Alex Chen')
  })

  it('leaves relations out of the one-liner', () => {
    const summary = workItemPacketSummary(item({ linked_keys: ['PHX-300'], parent_key: 'PHX-100' }))
    expect(summary).toBe('story · open · parent PHX-100')
  })

  it('stays under the server-side packet summary cap', () => {
    const summary = workItemPacketSummary(
      item({ labels: Array.from({ length: 32 }, (_, index) => 'l'.repeat(255) + index) }),
    )
    expect(summary.length).toBeLessThanOrEqual(4_000)
    expect(summary.endsWith('…')).toBe(true)
  })

  it('never truncates through the middle of an astral character', () => {
    const hasLoneSurrogate = (text: string): boolean => {
      for (let index = 0; index < text.length; index++) {
        const code = text.charCodeAt(index)
        if (code >= 0xd800 && code <= 0xdbff) {
          const next = text.charCodeAt(index + 1)
          if (!(next >= 0xdc00 && next <= 0xdfff)) return true
          index++
        } else if (code >= 0xdc00 && code <= 0xdfff) {
          return true
        }
      }
      return false
    }
    // Slide an emoji-only payload across the clip boundary: whichever parity
    // the cap lands on, the result must stay well-formed (no lone surrogate,
    // which fetch would mangle to U+FFFD in the launched packet).
    for (const offset of [0, 1]) {
      const summary = workItemPacketSummary(
        item({ labels: ['x'.repeat(offset) + '💥'.repeat(2_100)] }),
      )
      expect(summary.length).toBeLessThanOrEqual(4_000)
      expect(hasLoneSurrogate(summary)).toBe(false)
    }
  })
})
