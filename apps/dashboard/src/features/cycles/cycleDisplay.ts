import type { ConsumerInfo } from '@/api/apexClient'
import type { Cycle, Review } from './api'

export const REVIEW_LABELS: Record<Review['action'], string> = {
  submit: 'Submit for review', approve: 'Approve this version',
  request_revision: 'Request revision', archive: 'Archive in APEX', restore: 'Restore in APEX',
}
export const REVIEW_EFFECTS: Record<Review['action'], string> = {
  submit: 'Send this exact version to reviewers. Further changes require a revised draft.',
  approve: 'Record approval of this exact version and its frozen dependencies. This approval cannot be edited or transferred to a revised draft.',
  request_revision: 'Return this version to draft with your note so its author can address the requested changes.',
  archive: 'Remove this version from active use in APEX. Its content and review history are retained; it can be restored.',
  restore: 'Restore this version to active use. Existing dependencies and approval freshness will be checked again.',
}
export const REVIEW_RESULTS: Record<Review['action'], string> = {
  submit: 'Submitted for review', approve: 'Version approved', request_revision: 'Revision requested',
  archive: 'Version archived', restore: 'Version restored',
}

export function actorLabel(id: string | undefined, consumer: ConsumerInfo | null | undefined): string {
  if (!id) return 'Identity not recorded'
  return consumer?.id === id ? consumer.name : id
}

export function cycleNextAction(cycle: Cycle): string {
  for (const [kind, label] of [['scope', 'scope'], ['test_plan', 'test plan'], ['readiness', 'readiness'], ['script', 'script']] as const) {
    const latest = cycle.versions.filter((version) => version.kind === kind && !version.archived).at(-1)
    if (!latest) return `Create a ${label} draft`
    if (latest.status === 'draft') return `Complete and submit the ${label} for review`
    if (latest.status === 'in_review') return `Review the ${label} version`
    if (!latest.approval_current) return `Update stale ${label} evidence and request a new approval`
  }
  return cycle.blockers[0] ?? 'Prepare execution and review preflight checks'
}

export function fieldValue(value: unknown): string {
  return value == null || (typeof value === 'string' && !value.trim()) ? 'Not provided' : String(value)
}
