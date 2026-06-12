import { describe, expect, it } from 'vitest'

import { artifactKeyFromUri, artifactProxyUrl } from './artifactUrl'

describe('artifactKeyFromUri', () => {
  it('treats everything after memory:// as the store key (no host segment)', () => {
    expect(artifactKeyFromUri('memory://transcripts/execution/attempt-1')).toBe(
      'transcripts/execution/attempt-1',
    )
    expect(artifactKeyFromUri('memory://single-key')).toBe('single-key')
  })

  it('strips the bucket from s3:// uris and keeps the rest as the key', () => {
    expect(artifactKeyFromUri('s3://apex-artifacts/reports/thread-1/load-report.json')).toBe(
      'reports/thread-1/load-report.json',
    )
  })

  it('rejects malformed and unsupported uris', () => {
    expect(artifactKeyFromUri('memory://')).toBeNull()
    expect(artifactKeyFromUri('s3://bucket-only')).toBeNull()
    expect(artifactKeyFromUri('s3://bucket/')).toBeNull()
    expect(artifactKeyFromUri('file:///tmp/whatever')).toBeNull()
    expect(artifactKeyFromUri('https://example.com/x')).toBeNull()
    expect(artifactKeyFromUri(undefined)).toBeNull()
    expect(artifactKeyFromUri(null)).toBeNull()
    expect(artifactKeyFromUri('')).toBeNull()
  })
})

describe('artifactProxyUrl', () => {
  it('builds the same-origin /v1/artifacts proxy URL with literal slashes', () => {
    expect(artifactProxyUrl('memory://transcripts/execution/attempt-1')).toBe(
      `${window.location.origin}/v1/artifacts/transcripts/execution/attempt-1`,
    )
    expect(artifactProxyUrl('s3://apex-artifacts/reports/r-1.json')).toBe(
      `${window.location.origin}/v1/artifacts/reports/r-1.json`,
    )
  })

  it('percent-encodes within segments but never the separators', () => {
    expect(artifactProxyUrl('memory://reports/with space/file.json')).toBe(
      `${window.location.origin}/v1/artifacts/reports/with%20space/file.json`,
    )
  })

  it('returns null for non-proxyable uris', () => {
    expect(artifactProxyUrl('https://example.com/x')).toBeNull()
    expect(artifactProxyUrl(undefined)).toBeNull()
  })
})
