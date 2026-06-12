/**
 * Pure helpers for the admin screens (D7) — kind grouping/labels, scope
 * summaries and the shared options-JSON parser. No React imports.
 */
import { PORT_KINDS, type Connection, type PortKind } from '@/api/hooks/useConnections'
import type { ScopeRef } from '@/api/hooks/useConsumers'

export interface ConnectionGroup {
  kind: PortKind
  connections: Connection[]
}

/** "work_tracking" -> "Work tracking" for kind headings/chips. */
export function kindLabel(kind: PortKind): string {
  const words = kind.replaceAll('_', ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** Groups the registry by kind in canonical PortKind order; empty kinds drop out. */
export function groupConnectionsByKind(connections: Connection[]): ConnectionGroup[] {
  return PORT_KINDS.map((kind) => ({
    kind,
    connections: connections.filter((connection) => connection.kind === kind),
  })).filter((group) => group.connections.length > 0)
}

/** "demo" or "demo/app1" for one ScopeRef. */
export function scopeLabel(scope: ScopeRef): string {
  return scope.app_id ? `${scope.project_id}/${scope.app_id}` : scope.project_id
}

/** Compact table summary: '—', 'demo', or 'demo/app1 +2'. */
export function scopesSummary(scopes: ScopeRef[]): string {
  const [first, ...rest] = scopes
  if (!first) return '—'
  return rest.length === 0 ? scopeLabel(first) : `${scopeLabel(first)} +${rest.length}`
}

export type JsonObjectParse =
  | { ok: true; value: Record<string, unknown> }
  | { ok: false; message: string }

/** Options must parse as a JSON object (not array/scalar) before submit enables. */
export function parseJsonObject(text: string): JsonObjectParse {
  const trimmed = text.trim()
  if (trimmed === '') return { ok: true, value: {} }
  try {
    const value: unknown = JSON.parse(trimmed)
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
      return { ok: false, message: 'Options must be a JSON object.' }
    }
    return { ok: true, value: value as Record<string, unknown> }
  } catch {
    return { ok: false, message: 'Options is not valid JSON.' }
  }
}
