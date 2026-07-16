import { describe, expect, it } from 'vitest'

import { safeExternalHttpUrl } from './safeExternalUrl'

describe('safeExternalHttpUrl', () => {
  it.each([
    'javascript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'https://user:secret@example.test/item',
    '/relative/item',
    ' https://example.test/item',
  ])('rejects unsafe provider URL %s', (value) => {
    expect(safeExternalHttpUrl(value)).toBeNull()
  })

  it('normalizes absolute HTTP(S) URLs', () => {
    expect(safeExternalHttpUrl('https://tracker.example.test/items/ABC-1')).toBe(
      'https://tracker.example.test/items/ABC-1',
    )
    expect(safeExternalHttpUrl('http://localhost:8080/item')).toBe(
      'http://localhost:8080/item',
    )
  })
})
