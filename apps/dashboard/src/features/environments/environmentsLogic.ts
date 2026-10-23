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
  /** Projects the application is linked to (empty for unknown apps). */
  projects: string[]
  /** The application record, when it is visible (description, archived state). */
  application?: Application
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
    const list = byApp.get(app.id) ?? []
    byApp.delete(app.id)
    groups.push({
      key: app.id,
      label: app.name,
      projects: app.project_ids,
      application: app,
      environments: list.sort((a, b) => a.name.localeCompare(b.name)),
    })
  }
  // Environments whose application is not in the visible index (shouldn't
  // happen under normal scoping) still render, keyed by the raw id.
  for (const [appId, list] of byApp) {
    groups.push({
      key: appId,
      label: appId,
      projects: [],
      environments: list.sort((a, b) => a.name.localeCompare(b.name)),
    })
  }
  return groups
}

/** Readable kind labels; the stored values stay k8s / vm / other. */
export const KIND_LABELS: Record<string, string> = { k8s: 'Kubernetes', vm: 'Virtual machines', other: 'Other' }

export function kindLabel(kind: string | null | undefined): string | null {
  return kind ? KIND_LABELS[kind] ?? kind : null
}

/** Whether external engines can run against an environment. */
export type TargetStatus = 'approved' | 'needs-approval' | 'no-target'

export const TARGET_STATUS_LABELS: Record<TargetStatus, string> = {
  'approved': 'Approved',
  'needs-approval': 'Needs approval',
  'no-target': 'No target',
}

export function targetStatus(environment: Pick<Environment, 'base_url' | 'target_approved'>): TargetStatus {
  if (!environment.base_url) return 'no-target'
  return environment.target_approved ? 'approved' : 'needs-approval'
}

/** The host part of a target URL for compact display; the raw value if it doesn't parse. */
export function targetHost(url: string): string {
  try {
    return new URL(url).host || url
  } catch {
    return url
  }
}

export type ReadinessFilter = '' | TargetStatus

/**
 * Applications narrowed by the search text and readiness filter. An application
 * whose name (or one of its projects) matches keeps all its environments;
 * otherwise only matching environments are listed.
 */
export function filterGroups(
  groups: EnvironmentGroup[],
  { search = '', readiness = '', projectName = () => '' }: {
    search?: string
    readiness?: ReadinessFilter
    /** Display name of a project, so searching "ACC Perf" finds its applications. */
    projectName?: (projectId: string) => string
  } = {},
): EnvironmentGroup[] {
  const needle = search.trim().toLowerCase()
  const result: EnvironmentGroup[] = []
  for (const group of groups) {
    const haystack = [group.label, ...group.projects, ...group.projects.map(projectName)].join(' ')
    const appMatches = !needle || haystack.toLowerCase().includes(needle)
    const environments = group.environments.filter(environment =>
      (appMatches || `${environment.name} ${environment.base_url ?? ''}`.toLowerCase().includes(needle)) &&
      (!readiness || targetStatus(environment) === readiness))
    // Keep an application with no environments visible unless a filter excludes it.
    const keep = environments.length > 0 || (group.environments.length === 0 && appMatches && !readiness)
    if (keep) result.push({ ...group, environments })
  }
  return result
}

/** What the signed-in user may do with project links (create/edit dialogs). */
export interface ProjectChoices {
  /** Projects offered as checkboxes. */
  options: string[]
  /** Whether the user may link or unlink this project. */
  canChange: (projectId: string) => boolean
  /** Unscoped administrators may type a project ID that has no applications yet. */
  allowOther: boolean
  caption: (projectId: string) => string
}
