import { describe, expect, it } from 'vitest'

import type { DocumentOut } from '@/api/hooks/useDocuments'

import {
  isAcceptedContextFile,
  parseStatusBadge,
  summarizeContext,
  validateContextFile,
} from '../contextFiles'

function doc(overrides: Partial<DocumentOut>): DocumentOut {
  return {
    id: 'd',
    name: 'd.txt',
    media_type: 'text/plain',
    size_bytes: 1,
    artifact_key: 'documents/d',
    ...overrides,
  }
}

describe('isAcceptedContextFile', () => {
  it.each(['spec.pdf', 'NOTES.DOCX', 'readme.md', 'a.markdown', 'plain.txt'])(
    'accepts %s',
    (name) => {
      expect(isAcceptedContextFile(name)).toBe(true)
    },
  )

  it.each(['diagram.png', 'archive.zip', 'legacy.doc', 'noext'])('rejects %s', (name) => {
    expect(isAcceptedContextFile(name)).toBe(false)
  })
})

describe('validateContextFile', () => {
  it('returns null for an accepted file', () => {
    expect(validateContextFile(new File(['x'], 'spec.pdf'))).toBeNull()
  })

  it('returns a friendly error naming the file for an unaccepted type', () => {
    const message = validateContextFile(new File(['x'], 'photo.png'))
    expect(message).toMatch(/photo\.png/)
    expect(message).toMatch(/unsupported type/i)
  })
})

describe('parseStatusBadge', () => {
  it('maps parsed to an included success badge', () => {
    expect(parseStatusBadge('parsed')).toEqual({ label: 'Parsed', tone: 'success', included: true })
  })

  it('maps failed and unsupported to non-included badges', () => {
    expect(parseStatusBadge('failed').included).toBe(false)
    expect(parseStatusBadge('failed').tone).toBe('danger')
    expect(parseStatusBadge('unsupported').included).toBe(false)
    expect(parseStatusBadge('unsupported').tone).toBe('warning')
  })

  it('falls back to a muted pending badge for unknown/empty status', () => {
    expect(parseStatusBadge(null)).toEqual({ label: 'Pending', tone: 'muted', included: false })
  })
})

describe('summarizeContext', () => {
  it('counts parsed docs, sums their chars, and counts unreadable ones', () => {
    const summary = summarizeContext([
      doc({ id: 'a', parse_status: 'parsed', extracted_chars: 100 }),
      doc({ id: 'b', parse_status: 'parsed', extracted_chars: 250 }),
      doc({ id: 'c', parse_status: 'failed' }),
      doc({ id: 'd', parse_status: 'unsupported' }),
      undefined, // not yet resolved from the list
    ])
    expect(summary).toEqual({ includedCount: 2, totalChars: 350, unreadableCount: 2 })
  })

  it('treats a missing extracted_chars as zero', () => {
    const summary = summarizeContext([doc({ parse_status: 'parsed' })])
    expect(summary).toEqual({ includedCount: 1, totalChars: 0, unreadableCount: 0 })
  })
})
