import type { PhaseName } from '@apex/pipeline-events'

export const TELECOM_DATASET_VERSION = 'acc-add-line-demo-2.0'
export const TELECOM_PROJECT_ID = 'telecom-modernization'
export const FLAGSHIP_ALIGN_QUERY =
  "programId eq 42 and itemtype in ('feature','story') and state ne 'Accepted' and contains(title,'ACC add-a-line')"

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
  prompts: 21,
  seededRuns: 8,
  logs: 40,
  phaseMetricEvents: 56,
  usageDays: 14,
  artifacts: 32,
} as const

export function deterministicAgentReply(phase: PhaseName, message: string): string {
  const lower = message.toLowerCase()
  if (/mq|queue|consumer/.test(lower)) {
    return 'The queue signal is isolated to fulfillment: depth rises after 200 concurrent line additions because only two consumers are active. I recommend four consumers and a bounded ramp hold.'
  }
  if (/sla|release|risk/.test(lower)) {
    return 'The orchestration completed, but BSSe p95 is 650ms above the release SLA. Treat this as a conditional certification and track remediation before promotion.'
  }
  if (/oracle|configuration|config|endpoint/.test(lower)) {
    return 'The PERFHYPER2 inventory confirms mulesoft-add-line-api is using a non-PERFHYPER2 BSSe test endpoint. I can apply the simulated PERFHYPER2 mapping, rescan, and preserve this finding in the audit trail.'
  }
  return `For ${phase.replaceAll('_', ' ')}, I recorded your request and regenerated the deterministic result using the approved telecom evidence packet.`
}

export const ACC_DEMO_DEPENDENCIES = [
  { application: 'Mulesoft', environment: 'TEST', base_url: 'https://mulesoft.perfhyper2.example.test', purpose: 'ACC account, eligibility, quote, and add-line integration' },
  { application: 'BSSe', environment: 'TEST', base_url: 'https://bsse.perfhyper2.example.test', purpose: 'Existing cellular plan, line order, activation status, and billing confirmation' },
] as const

export const ACC_DEMO_CONTEXT = `ACC is a web-based application employees use to sell mobile phones, cellular plans, and internet service. This demo tests adding one line to an existing customer's cellular plan in PERFHYPER2, connected to Mulesoft and BSSe test environments.
An authorized employee signs in to ACC, finds a synthetic existing customer, opens the active cellular plan, checks account eligibility and the plan line limit, selects Add a line, chooses a compatible phone or BYOD device and SIM, selects the line offer, reviews recurring and one-time charges, submits once, and verifies the order and new line in ACC and BSSe through Mulesoft. Existing lines and plan entitlements must remain unchanged. Internet sales and device replacement are outside this test scope.
Assert one order, one new line, and one set of charges after a retry using the same idempotency key. Cover ineligible or suspended accounts, maximum line count, incompatible devices, unavailable SIM inventory, quote changes, expired employee sessions, concurrent submissions, Mulesoft timeout/429/5xx, BSSe rejection, and delayed activation. Rejections must not leave a partial line or charge. Poll pending orders with bounded backoff and reconcile ambiguous outcomes before retrying.
All accounts, endpoints, infrastructure, traffic, thresholds, and results are synthetic demo assumptions, not verified environment configuration or measured production behavior. Use exclusive account leases, unique SIMs and request keys, reset added lines and test charges after each run, and verify cleanup. Event times are relative to demo activation.`

export const ACC_DEMO_TEST_PLAN = `# ACC add-a-line performance plan — PERFHYPER2

${ACC_DEMO_CONTEXT}

## Workload (illustrative, requires validation before a real run)
Run a one-employee smoke flow first. Ramp to 100, 200, and 300 concurrent employee sessions over 15 minutes, hold peak for 10 minutes, then drain pending orders. Model 5–15 seconds of employee think time between screens. The 85 TPS target is aggregate API traffic, not 85 completed line additions per second; report completed journeys separately. Keep the functional rejection matrix separate from the successful-journey error denominator. Compare against the two seeded synthetic baselines.

## Checks and acceptance
Measure employee sign-in, account lookup, eligibility, offers, quote, submission, and confirmation separately; correlate sessionId, accountId, quoteId, orderId, lineId, and idempotency key across ACC, Mulesoft, and BSSe. Demo submission p95 budget: 3,000ms; unexpected error rate below 2%; aggregate throughput at least 80 TPS. Zero duplicate orders, duplicate charges, unauthorized changes, or changes to existing lines are permitted. Report activation completion and outstanding orders separately from submission latency. Abort on data corruption; hold the ramp for sustained dependency saturation and drain before resuming.

## Evidence and cleanup
Capture request correlation IDs, dependency timings, final line counts, expected versus actual charges, rejection outcomes, and account/SIM lease records. Remove only this run's synthetic lines and charges, release inventory, verify the original plan and lines, and record any cleanup failures. Reports are simulated and cannot certify a real release.`

export const ACC_DEMO_TEST_DATA = `account_id,plan_id,account_status,current_lines,max_lines,device_id,sim_id,offer_id,expected_monthly_charge,expected_one_time_charge,expected_result
SYN-1001,PLAN-FAMILY-01,active,2,5,DEV-2001,SIM-3001,OFFER-LINE-01,25.00,10.00,one_new_line
SYN-1002,PLAN-FAMILY-01,active,5,5,DEV-2002,SIM-3002,OFFER-LINE-01,0.00,0.00,line_limit_rejected
SYN-1003,PLAN-FAMILY-01,suspended,2,5,DEV-2003,SIM-3003,OFFER-LINE-01,0.00,0.00,ineligible_account_rejected
SYN-1004,PLAN-FAMILY-01,active,1,5,DEV-INCOMPATIBLE,SIM-3004,OFFER-LINE-01,0.00,0.00,incompatible_device_rejected
`
