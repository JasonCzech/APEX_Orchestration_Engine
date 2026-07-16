import { z } from 'zod'

import { fetchWithoutRedirects } from '@/api/fetchPolicy'

const rawRuntimeConfigSchema = z
  .object({
    /** Origin serving the APEX /v1 domain API. Empty string = same origin (vite proxy in dev, reverse proxy in prod). */
    apexOrigin: z.string().default(''),
    /** Origin serving the LangGraph Assistants/Threads/Runs API. Empty string = same origin. */
    langgraphOrigin: z.string().default(''),
  })
  .strict()

export type RuntimeConfig = z.infer<typeof rawRuntimeConfigSchema>

export const DEFAULT_RUNTIME_CONFIG: RuntimeConfig = {
  apexOrigin: '',
  langgraphOrigin: '',
}

let current: RuntimeConfig = DEFAULT_RUNTIME_CONFIG

const MAX_RUNTIME_CONFIG_BYTES = 16 * 1024
export const MAX_RUNTIME_CONFIG_CHUNKS = 256
export const RUNTIME_CONFIG_DEADLINE_MS = 5_000
const INVALID_RUNTIME_CONFIG = 'Dashboard runtime configuration is invalid.'

const HTTP_HOST = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$/

function isValidHttpHostname(hostname: string): boolean {
  // WHATWG URL parsing accepts and canonicalizes bracketed IPv6 literals for
  // special HTTP(S) URLs, while rejecting invalid literals and scoped zone
  // identifiers. DNS names retain the stricter bounded-label validation.
  if (hostname.startsWith('[') && hostname.endsWith(']')) {
    return hostname.length <= 41 && !hostname.includes('%')
  }
  return hostname.length <= 253 && HTTP_HOST.test(hostname)
}

function responseMediaType(response: Response): string {
  return (response.headers.get('content-type') ?? '').split(';', 1)[0]?.trim().toLowerCase() ?? ''
}

function cancelResponseBody(response: Response): void {
  // Cancellation is advisory cleanup. A broken/malicious stream must not keep
  // dashboard bootstrap waiting after the response is already rejected.
  void response.body?.cancel().catch(() => undefined)
}

function cancelReader(reader: ReadableStreamDefaultReader<Uint8Array>): void {
  try {
    void reader.cancel().catch(() => undefined)
  } catch {
    // Cancellation is advisory; callers already return a stable outcome.
  }
}

function readChunk(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  signal: AbortSignal,
): Promise<ReadableStreamReadResult<Uint8Array>> {
  if (signal.aborted) return Promise.reject(new Error(INVALID_RUNTIME_CONFIG))
  return new Promise((resolve, reject) => {
    const onAbort = (): void => reject(new Error(INVALID_RUNTIME_CONFIG))
    signal.addEventListener('abort', onAbort, { once: true })
    void reader.read().then(
      (value) => {
        signal.removeEventListener('abort', onAbort)
        resolve(value)
      },
      () => {
        signal.removeEventListener('abort', onAbort)
        reject(new Error(INVALID_RUNTIME_CONFIG))
      },
    )
  })
}

function normalizeHttpOrigin(value: string): string | null {
  if (value === '') return ''
  if (value !== value.trim() || value.length > 2_048) return null
  try {
    const parsed = new URL(value)
    if (
      (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') ||
      parsed.username !== '' ||
      parsed.password !== '' ||
      parsed.pathname !== '/' ||
      parsed.search !== '' ||
      parsed.hash !== '' ||
      !isValidHttpHostname(parsed.hostname)
    ) {
      return null
    }
    const port = parsed.port === '' ? null : Number(parsed.port)
    if (port !== null && (!Number.isInteger(port) || port < 1 || port > 65_535)) return null
    return parsed.origin
  } catch {
    return null
  }
}

function parseRuntimeConfig(value: unknown): RuntimeConfig {
  const parsed = rawRuntimeConfigSchema.safeParse(value)
  if (!parsed.success) throw new Error(INVALID_RUNTIME_CONFIG)
  const apexOrigin = normalizeHttpOrigin(parsed.data.apexOrigin)
  const langgraphOrigin = normalizeHttpOrigin(parsed.data.langgraphOrigin)
  if (apexOrigin === null || langgraphOrigin === null) {
    throw new Error(INVALID_RUNTIME_CONFIG)
  }
  return { apexOrigin, langgraphOrigin }
}

async function readRuntimeConfig(response: Response, signal: AbortSignal): Promise<unknown> {
  const declaredLength = response.headers.get('content-length')
  if (
    declaredLength !== null &&
    (!/^[0-9]+$/.test(declaredLength) || Number(declaredLength) > MAX_RUNTIME_CONFIG_BYTES)
  ) {
    cancelResponseBody(response)
    throw new Error(INVALID_RUNTIME_CONFIG)
  }
  if (!response.body) throw new Error(INVALID_RUNTIME_CONFIG)

  const reader = response.body.getReader()
  const chunks: Uint8Array[] = []
  let total = 0
  let chunkCount = 0
  try {
    while (true) {
      const chunk = await readChunk(reader, signal)
      if (chunk.done) break
      chunkCount += 1
      if (
        chunkCount > MAX_RUNTIME_CONFIG_CHUNKS ||
        total + chunk.value.byteLength > MAX_RUNTIME_CONFIG_BYTES
      ) {
        // Cancellation is advisory cleanup. Some broken streams never settle
        // the cancellation promise; validation must reject without waiting.
        cancelReader(reader)
        throw new Error(INVALID_RUNTIME_CONFIG)
      }
      chunks.push(chunk.value)
      total += chunk.value.byteLength
    }
  } catch {
    cancelReader(reader)
    throw new Error(INVALID_RUNTIME_CONFIG)
  } finally {
    try {
      reader.releaseLock()
    } catch {
      // A hostile pending read can retain the lock until cancellation settles.
    }
  }

  const body = new Uint8Array(total)
  let offset = 0
  for (const chunk of chunks) {
    body.set(chunk, offset)
    offset += chunk.byteLength
  }
  try {
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(body)) as unknown
  } catch {
    throw new Error(INVALID_RUNTIME_CONFIG)
  }
}

export function setRuntimeConfig(config: RuntimeConfig): RuntimeConfig {
  current = parseRuntimeConfig(config)
  return current
}

export function getRuntimeConfig(): RuntimeConfig {
  return current
}

/**
 * Fetch the runtime configuration (/config.json) before mounting the app.
 * An unavailable or non-JSON config falls back to same-origin defaults — the
 * dev server proxies /v1 and the LangGraph API paths, and production deploys
 * sit behind a same-origin reverse proxy. An explicit JSON configuration is a
 * trusted deployment boundary, so malformed values fail closed.
 */
export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  const controller = new AbortController()
  let timedOut = false
  const deadline = new Promise<never>((_resolve, reject) => {
    const timer = setTimeout(() => {
      timedOut = true
      controller.abort()
      reject(new Error(INVALID_RUNTIME_CONFIG))
    }, RUNTIME_CONFIG_DEADLINE_MS)
    controller.signal.addEventListener('abort', () => clearTimeout(timer), { once: true })
  })
  let response: Response
  try {
    response = await Promise.race([
      fetchWithoutRedirects('/config.json', { cache: 'no-store', signal: controller.signal }),
      deadline,
    ])
  } catch {
    controller.abort()
    return setRuntimeConfig(DEFAULT_RUNTIME_CONFIG)
  }
  try {
    if (response.redirected || !response.ok || responseMediaType(response) !== 'application/json') {
      cancelResponseBody(response)
      return setRuntimeConfig(DEFAULT_RUNTIME_CONFIG)
    }
    const value = await readRuntimeConfig(response, controller.signal)
    current = parseRuntimeConfig(value)
    return current
  } catch {
    throw new Error(INVALID_RUNTIME_CONFIG)
  } finally {
    if (!timedOut) controller.abort()
  }
}

/** APEX API base URL — origin only; the generated schema paths already carry the /v1 prefix. */
export function resolveApexBaseUrl(): string {
  if (current.apexOrigin) return current.apexOrigin
  return typeof window !== 'undefined' ? window.location.origin : ''
}

export function resolveLanggraphBaseUrl(): string {
  if (current.langgraphOrigin) return current.langgraphOrigin
  return typeof window !== 'undefined' ? window.location.origin : ''
}
