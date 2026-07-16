import { describe, expect, it } from 'vitest'

import { queryKeys } from './queryKeys'

describe('query keys', () => {
  it('keeps route details disjoint from id lookups for user-controlled namespaces', () => {
    expect(queryKeys.prompts.detail('by-id', 'prompt-123')).toEqual([
      'prompts',
      'detail',
      'by-id',
      'prompt-123',
    ])
    expect(queryKeys.prompts.byId('prompt-123')).toEqual([
      'prompts',
      'by-id',
      'prompt-123',
    ])
    expect(queryKeys.prompts.detail('by-id', 'prompt-123')).not.toEqual(
      queryKeys.prompts.byId('prompt-123'),
    )
  })

  it('keeps version entries beneath their tagged route detail', () => {
    expect(queryKeys.prompts.versions('phase', 'story/system')).toEqual([
      ...queryKeys.prompts.detail('phase', 'story/system'),
      'versions',
    ])
    expect(queryKeys.prompts.version('phase', 'story/system', 2)).toEqual([
      ...queryKeys.prompts.versions('phase', 'story/system'),
      '2',
    ])
  })

  it('keeps golden-config detail caches disjoint from reserved list markers', () => {
    expect(queryKeys.goldenConfigs.detail('list')).not.toEqual(
      queryKeys.goldenConfigs.list(),
    )
    expect(queryKeys.goldenConfigs.detail('index')).not.toEqual(
      queryKeys.goldenConfigs.index(),
    )
  })

  it('keeps environment details disjoint from the reserved index marker', () => {
    expect(queryKeys.catalog.environment('index')).not.toEqual(
      queryKeys.catalog.environmentsIndex(),
    )
  })

  it('keeps document and draft details disjoint from list markers', () => {
    expect(queryKeys.documents.detail('list')).not.toEqual(queryKeys.documents.list())
    expect(queryKeys.drafts.detail('list')).not.toEqual(queryKeys.drafts.list())
  })

  it('keeps work-item detail caches disjoint across exact connection bindings', () => {
    expect(queryKeys.workItems.key('PHX-101', 'proj-alpha', 'conn-jira', 'jira')).not.toEqual(
      queryKeys.workItems.key('PHX-101', 'proj-alpha', 'conn-jira-dr', 'jira'),
    )
    expect(queryKeys.workItems.key('PHX-101', 'proj-alpha', 'conn-jira', 'jira')).toEqual([
      'work-items',
      'key',
      'PHX-101',
      { project: 'proj-alpha', connection: 'conn-jira', provider: 'jira' },
    ])
  })
})
