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
import { notifyUnauthorized } from '@/api/apexClient'
import {
  getApiKey,
  getApiKeyRevision,
  getSessionRevision,
  subscribeApiKey,
  subscribeSession,
} from '@/auth/keyStorage'
import { resolveApexBaseUrl } from '@/config/runtimeConfig'
import { getDevArtifactBytes } from '@/dev-data'

import { ApiError } from '@/api/errors'
import { fetchWithoutRedirects } from '@/api/fetchPolicy'

const MEMORY_SCHEME = 'memory://'
const S3_SCHEME = 's3://'
const CANONICAL_SCHEME = 'apex-artifact:///'
export const MAX_ARTIFACT_VIEWER_BYTES = 64 * 1024 * 1024
export const MAX_ARTIFACT_VIEWER_CHUNKS = 8_192
export const ARTIFACT_READ_IDLE_MS = 30_000
export const ARTIFACT_READ_ERROR = 'Artifact could not be loaded safely.'

function containsControlCharacter(value: string): boolean {
  return Array.from(value).some((character) => {
    const codePoint = character.codePointAt(0)
    return codePoint !== undefined && (codePoint <= 0x1f || codePoint === 0x7f)
  })
}

function withArtifactReadDeadline<T>(
  operation: Promise<T>,
  controller: AbortController,
  timeoutMs = ARTIFACT_READ_IDLE_MS,
): Promise<T> {
  if (controller.signal.aborted) return Promise.reject(new Error(ARTIFACT_READ_ERROR))
  return new Promise((resolve, reject) => {
    const rejectSafely = (): void => {
      clearTimeout(timer)
      controller.signal.removeEventListener('abort', rejectSafely)
      reject(new Error(ARTIFACT_READ_ERROR))
    }
    const timer = setTimeout(() => {
      controller.abort()
      rejectSafely()
    }, timeoutMs)
    controller.signal.addEventListener('abort', rejectSafely, { once: true })
    void operation.then(
      (value) => {
        clearTimeout(timer)
        controller.signal.removeEventListener('abort', rejectSafely)
        resolve(value)
      },
      rejectSafely,
    )
  })
}

function cancelBody(body: ReadableStream<Uint8Array> | null): void {
  try {
    void body?.cancel().catch(() => undefined)
  } catch {
    // Cancellation is advisory; the stable rejection must never wait on it.
  }
}

function cancelReader(reader: ReadableStreamDefaultReader<Uint8Array>): void {
  try {
    void reader.cancel().catch(() => undefined)
  } catch {
    // Cancellation is advisory; the stable rejection must never wait on it.
  }
}

/** Read a binary response without ever buffering more than the viewer budget. */
export async function readBoundedArtifactBody(
  response: Response,
  maxBytes = MAX_ARTIFACT_VIEWER_BYTES,
  maxChunks = MAX_ARTIFACT_VIEWER_CHUNKS,
  controller = new AbortController(),
): Promise<Blob> {
  const mediaType = response.headers.get('content-type') ?? ''
  const declaredLength = response.headers.get('content-length')
  if (
    !Number.isSafeInteger(maxBytes) ||
    maxBytes < 0 ||
    !Number.isSafeInteger(maxChunks) ||
    maxChunks < 1 ||
    (declaredLength !== null &&
      (!/^(?:0|[1-9][0-9]*)$/.test(declaredLength) ||
        !Number.isSafeInteger(Number(declaredLength)) ||
        Number(declaredLength) > maxBytes))
  ) {
    cancelBody(response.body)
    throw new Error(ARTIFACT_READ_ERROR)
  }
  if (!response.body) return new Blob([], { type: mediaType })

  const reader = response.body.getReader()
  const chunks: ArrayBuffer[] = []
  let total = 0
  let chunkCount = 0
  try {
    while (true) {
      const chunk = await withArtifactReadDeadline(reader.read(), controller)
      if (chunk.done) break
      chunkCount += 1
      if (chunkCount > maxChunks || chunk.value.byteLength > maxBytes - total) {
        cancelReader(reader)
        throw new Error(ARTIFACT_READ_ERROR)
      }
      const copy = new Uint8Array(chunk.value.byteLength)
      copy.set(chunk.value)
      chunks.push(copy.buffer)
      total += chunk.value.byteLength
    }
  } catch {
    controller.abort()
    cancelReader(reader)
    throw new Error(ARTIFACT_READ_ERROR)
  } finally {
    try {
      reader.releaseLock()
    } catch {
      // A hostile pending cancellation can retain the lock; rejection is final.
    }
  }
  return new Blob(chunks, { type: mediaType })
}

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
  if (uri.startsWith(CANONICAL_SCHEME)) {
    const encodedKey = uri.slice(CANONICAL_SCHEME.length)
    if (encodedKey.length === 0 || encodedKey.includes('?') || encodedKey.includes('#')) {
      return null
    }
    try {
      const key = decodeURIComponent(encodedKey)
      return key.length > 0 && !containsControlCharacter(key) ? key : null
    } catch {
      return null
    }
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
export async function fetchArtifactBytes(url: string, signal?: AbortSignal): Promise<ArtifactBytes> {
  let artifactUrl: URL
  try {
    const base = new URL(resolveApexBaseUrl() || window.location.origin, window.location.origin)
    artifactUrl = new URL(url, base)
    if (
      artifactUrl.origin !== base.origin ||
      artifactUrl.username !== '' ||
      artifactUrl.password !== '' ||
      !artifactUrl.pathname.startsWith('/v1/artifacts/') ||
      artifactUrl.pathname === '/v1/artifacts/' ||
      artifactUrl.search !== '' ||
      artifactUrl.hash !== '' ||
      artifactUrl.href.length > 4_096
    ) {
      throw new Error(ARTIFACT_READ_ERROR)
    }
  } catch {
    throw new Error(ARTIFACT_READ_ERROR)
  }

  const devBytes = getDevArtifactBytes(artifactUrl.href)
  if (devBytes) return devBytes

  const headers = new Headers()
  const key = getApiKey()
  const keyRevision = getApiKeyRevision()
  const sessionRevision = getSessionRevision()
  if (key) headers.set('x-api-key', key)
  const controller = new AbortController()
  const abortFromCaller = (): void => controller.abort()
  const abortFromSessionChange = (): void => controller.abort()
  const sessionIsCurrent = (): boolean =>
    key === getApiKey() &&
    keyRevision === getApiKeyRevision() &&
    sessionRevision === getSessionRevision()
  if (signal?.aborted) throw new Error(ARTIFACT_READ_ERROR)
  signal?.addEventListener('abort', abortFromCaller, { once: true })
  const unsubscribeKey = subscribeApiKey(abortFromSessionChange)
  const unsubscribeSession = subscribeSession(abortFromSessionChange)
  try {
    const responseOperation = fetchWithoutRedirects(artifactUrl, {
      headers,
      signal: controller.signal,
    })
    // A custom transport is allowed to ignore AbortSignal. If it resolves
    // after a credential/session transition, drain no bytes and release its
    // body even though the caller has already received the stable rejection.
    void responseOperation.then(
      (lateResponse) => {
        if (controller.signal.aborted || !sessionIsCurrent()) cancelBody(lateResponse.body)
      },
      () => undefined,
    )
    const response = await withArtifactReadDeadline(
      responseOperation,
      controller,
    )
    if (!sessionIsCurrent()) {
      cancelBody(response.body)
      throw new Error(ARTIFACT_READ_ERROR)
    }
    if (response.status === 401 && key !== null) notifyUnauthorized()
    if (!response.ok) {
      // Never parse or retain an untrusted error body: providers/proxies may omit
      // Content-Length and the status is sufficient for an operator-safe error.
      cancelBody(response.body)
      throw new ApiError(response.status, `Artifact request failed (${response.status})`)
    }
    const blob = await readBoundedArtifactBody(
      response,
      MAX_ARTIFACT_VIEWER_BYTES,
      MAX_ARTIFACT_VIEWER_CHUNKS,
      controller,
    )
    if (!sessionIsCurrent()) throw new Error(ARTIFACT_READ_ERROR)
    return {
      blob,
      mediaType: response.headers.get('content-type') ?? '',
      size: blob.size,
    }
  } finally {
    signal?.removeEventListener('abort', abortFromCaller)
    unsubscribeKey()
    unsubscribeSession()
  }
}
