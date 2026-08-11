import type { PhaseName } from '@apex/pipeline-events'

export const TELECOM_DATASET_VERSION = 'telecom-demo-1.0'
export const TELECOM_PROJECT_ID = 'telecom-modernization'
export const FLAGSHIP_ALIGN_QUERY =
  "programId eq 42 and itemtype in ('feature','story') and state ne 'Accepted' and contains(title,'eSIM upgrade')"

export const TELECOM_DATASET_REQUIREMENTS = {
  applications: 3,
  environments: 4,
  connections: 9,
  consumers: 4,
  documents: 6,
  evidencePackets: 12,
  jiraAlignItems: 12,
  initialJiraCloudItems: 8,
  savedQueries: 4,
  prompts: 20,
  seededRuns: 8,
  logs: 40,
  phaseMetricEvents: 56,
  usageDays: 14,
  artifacts: 32,
} as const

export function deterministicAgentReply(phase: PhaseName, message: string): string {
  const lower = message.toLowerCase()
  if (/mq|queue|consumer/.test(lower)) {
    return 'The queue signal is isolated to fulfillment: depth rises after 200 concurrent upgrades because only two consumers are active. I recommend four consumers and a bounded ramp hold.'
  }
  if (/sla|release|risk/.test(lower)) {
    return 'The orchestration completed, but OMS p95 is 650ms above the release SLA. Treat this as a conditional certification and track remediation before promotion.'
  }
  if (/oracle|configuration|config|endpoint/.test(lower)) {
    return 'The PERF inventory confirms order-mgmt-svc is using a QC Oracle endpoint. I can apply the simulated PERF mapping, rescan, and preserve this finding in the audit trail.'
  }
  return `For ${phase.replaceAll('_', ' ')}, I recorded your request and regenerated the deterministic result using the approved telecom evidence packet.`
}
