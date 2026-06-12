/**
 * ArtifactRef.uri -> /v1/artifacts proxy URL.
 *
 * Key semantics verified against the backend:
 * - src/apex/adapters/stubs/artifact_store.py  put() -> uri = `memory://{key}`
 *   (the WHOLE remainder after the scheme is the store key — `memory://` has no
 *   bucket/host segment, so URL() parsing would wrongly eat the first segment).
 * - src/apex/adapters/s3/artifact_store.py     put() -> uri = `s3://{bucket}/{key}`
 *   (key = everything after the bucket segment).
 * - src/apex/routers/artifacts.py              GET /artifacts/{key:path} — the
 *   key keeps its literal slashes in the path, so we encode per-segment and
 *   re-join with '/' rather than encoding the whole key.
 */
import { getApiKey } from '@/auth/keyStorage'
import { resolveApexBaseUrl } from '@/config/runtimeConfig'

import { ApiError, errorMessageOf } from '@/api/errors'

const MEMORY_SCHEME = 'memory://'
const S3_SCHEME = 's3://'

/** Extract the artifact-store key from a ref uri; null when unsupported/malformed. */
export function artifactKeyFromUri(uri: string | null | undefined): string | null {
  if (!uri) return null
  if (uri.startsWith(MEMORY_SCHEME)) {
    const key = uri.slice(MEMORY_SCHEME.length)
    return key.length > 0 ? key : null
  }
  if (uri.startsWith(S3_SCHEME)) {
    const rest = uri.slice(S3_SCHEME.length)
    const slash = rest.indexOf('/')
    if (slash <= 0) return null // no bucket/key separator
    const key = rest.slice(slash + 1)
    return key.length > 0 ? key : null
  }
  return null
}

/** Same-origin authenticated proxy URL for a ref uri; null when not proxyable. */
export function artifactProxyUrl(uri: string | null | undefined): string | null {
  const key = artifactKeyFromUri(uri)
  if (key === null) return null
  const encodedKey = key.split('/').map(encodeURIComponent).join('/')
  return `${resolveApexBaseUrl()}/v1/artifacts/${encodedKey}`
}

export interface ArtifactBytes {
  blob: Blob
  /** Response Content-Type (the proxy resolves it server-side), '' if absent. */
  mediaType: string
  size: number
}

/**
 * Binary-safe artifact fetch with the x-api-key header. Deliberately a plain
 * fetch instead of the openapi-fetch client: the generated client percent-
 * encodes path params wholesale, which would mangle the `{key:path}` slashes.
 */
export async function fetchArtifactBytes(url: string): Promise<ArtifactBytes> {
  const headers = new Headers()
  const key = getApiKey()
  if (key) headers.set('x-api-key', key)
  const response = await fetch(url, { headers })
  if (!response.ok) {
    let body: unknown
    try {
      body = await response.json()
    } catch {
      body = undefined
    }
    throw new ApiError(
      response.status,
      errorMessageOf(body, `Artifact request failed (${response.status})`),
      body,
    )
  }
  const blob = await response.blob()
  return {
    blob,
    mediaType: response.headers.get('content-type') ?? '',
    size: blob.size,
  }
}
