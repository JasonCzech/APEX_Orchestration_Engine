import { describe, expect, it } from 'vitest'

import { artifactViewerPath } from './artifactPaths'

describe('artifactViewerPath', () => {
  it('encodes opaque thread and artifact ids as independent path segments', () => {
    expect(artifactViewerPath('thread/one', 'report/with ?# delimiters')).toBe(
      '/runs/thread%2Fone/artifacts/report%2Fwith%20%3F%23%20delimiters',
    )
  })
})
