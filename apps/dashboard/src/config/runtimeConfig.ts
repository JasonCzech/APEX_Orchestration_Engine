import { z } from 'zod'

const runtimeConfigSchema = z.object({
  /** Origin serving the APEX /v1 domain API. Empty string = same origin (vite proxy in dev, reverse proxy in prod). */
  apexOrigin: z.string().default(''),
  /** Origin serving the LangGraph Assistants/Threads/Runs API. Empty string = same origin. */
  langgraphOrigin: z.string().default(''),
})

export type RuntimeConfig = z.infer<typeof runtimeConfigSchema>

export const DEFAULT_RUNTIME_CONFIG: RuntimeConfig = {
  apexOrigin: '',
  langgraphOrigin: '',
}

let current: RuntimeConfig = DEFAULT_RUNTIME_CONFIG

export function setRuntimeConfig(config: RuntimeConfig): RuntimeConfig {
  current = config
  return current
}

export function getRuntimeConfig(): RuntimeConfig {
  return current
}

/**
 * Fetch the runtime configuration (/config.json) before mounting the app.
 * Missing/invalid config falls back to same-origin defaults — the dev server
 * proxies /v1 and the LangGraph API paths, and production deploys sit behind
 * a same-origin reverse proxy, so empty origins are the common case.
 */
export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  try {
    const response = await fetch('/config.json', { cache: 'no-store' })
    const contentType = response.headers.get('content-type') ?? ''
    if (!response.ok || !contentType.includes('application/json')) {
      return setRuntimeConfig(DEFAULT_RUNTIME_CONFIG)
    }
    const parsed = runtimeConfigSchema.safeParse(await response.json())
    return setRuntimeConfig(parsed.success ? parsed.data : DEFAULT_RUNTIME_CONFIG)
  } catch {
    return setRuntimeConfig(DEFAULT_RUNTIME_CONFIG)
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
