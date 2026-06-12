/**
 * Route helpers for /prompts/* (D5). Prompt keys contain slashes
 * ('story_analysis/system') and the router pattern is the non-splat
 * `/prompts/:ns/:name`, so the FULL key is percent-encoded into the single
 * :name segment (encodeURIComponent — %2F survives as one segment). React
 * Router decodes params exactly once, so readers take useParams().name as-is
 * and must never decode again (keys with a literal % would double-decode).
 * Documented in ROUTES.md; all links must come through these helpers.
 */
import { useParams } from 'react-router'

export function promptPath(ns: string, key: string): string {
  return `/prompts/${encodeURIComponent(ns)}/${encodeURIComponent(key)}`
}

export function promptVersionPath(ns: string, key: string, versionId: string): string {
  return `${promptPath(ns, key)}/versions/${encodeURIComponent(versionId)}`
}

export function promptPlaygroundPath(ns: string, key: string): string {
  return `${promptPath(ns, key)}/playground`
}

export interface PromptRouteParams {
  ns: string
  /** The full prompt key, already decoded by the router (may contain slashes). */
  name: string
}

/** Reads :ns/:name off the current match; router decoding already applied. */
export function usePromptRouteParams(): PromptRouteParams {
  const { ns = '', name = '' } = useParams<{ ns: string; name: string }>()
  return { ns, name }
}
