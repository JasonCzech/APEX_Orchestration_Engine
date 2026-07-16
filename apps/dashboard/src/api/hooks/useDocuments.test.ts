import { describe, expect, it } from 'vitest'

import { documentsListQueryKey } from './useDocuments'

describe('documentsListQueryKey', () => {
  it('does not alias literal app ids with internal audience modes', () => {
    expect(documentsListQueryKey('project-1', undefined, undefined)).not.toEqual(
      documentsListQueryKey('project-1', undefined, 'all'),
    )
    expect(documentsListQueryKey('project-1', undefined, undefined)).not.toEqual(
      documentsListQueryKey('project-1', undefined, null),
    )
  })
})
