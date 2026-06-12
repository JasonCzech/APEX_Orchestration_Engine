/**
 * Pure helpers for the environments screens (D5): application grouping for the
 * list, host-row draft mapping, and the validated options-JSON parser shared
 * by the create panel and the detail edit form.
 */
import type { Application, Environment, HostIn, HostOut } from '@/api/hooks/useEnvironments'

export const KIND_OPTIONS = ['k8s', 'vm', 'other'] as const

/** Editable host row — strings only so inputs stay controlled. */
export interface HostDraft {
  hostname: string
  role: string
}

export function hostsToDrafts(hosts: HostOut[]): HostDraft[] {
  return hosts.map((host) => ({ hostname: host.hostname, role: host.role ?? '' }))
}

/** Drops blank rows and normalizes empty roles to null for the API payload. */
export function hostsToPayload(hosts: HostDraft[]): HostIn[] {
  return hosts
    .filter((host) => host.hostname.trim().length > 0)
    .map((host) => ({ hostname: host.hostname.trim(), role: host.role.trim() || null }))
}

export type OptionsParse =
  | { ok: true; value: Record<string, unknown> }
  | { ok: false; message: string }

/** Options must be a JSON object; blank input means {} (the API default). */
export function parseOptionsJson(text: string): OptionsParse {
  const trimmed = text.trim()
  if (!trimmed) return { ok: true, value: {} }
  try {
    const parsed: unknown = JSON.parse(trimmed)
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { ok: false, message: 'Options must be a JSON object.' }
    }
    return { ok: true, value: parsed as Record<string, unknown> }
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? `Invalid JSON: ${error.message}` : 'Invalid JSON.',
    }
  }
}

export interface EnvironmentGroup {
  key: string
  /** Application name, or the raw application_id when the app is not visible. */
  label: string
  /** Project caption under the group header (empty for unknown apps). */
  project: string
  environments: Environment[]
}

/** Join environments onto applications; unknown app ids still get a group. */
export function groupEnvironments(
  applications: Application[],
  environments: Environment[],
): EnvironmentGroup[] {
  const byApp = new Map<string, Environment[]>()
  for (const env of environments) {
    const list = byApp.get(env.application_id) ?? []
    list.push(env)
    byApp.set(env.application_id, list)
  }
  const groups: EnvironmentGroup[] = []
  for (const app of [...applications].sort((a, b) => a.name.localeCompare(b.name))) {
    const list = byApp.get(app.id)
    if (!list) continue
    byApp.delete(app.id)
    groups.push({
      key: app.id,
      label: app.name,
      project: app.project_id,
      environments: list.sort((a, b) => a.name.localeCompare(b.name)),
    })
  }
  // Environments whose application is not in the visible index (shouldn't
  // happen under normal scoping) still render, keyed by the raw id.
  for (const [appId, list] of byApp) {
    groups.push({
      key: appId,
      label: appId,
      project: '',
      environments: list.sort((a, b) => a.name.localeCompare(b.name)),
    })
  }
  return groups
}
