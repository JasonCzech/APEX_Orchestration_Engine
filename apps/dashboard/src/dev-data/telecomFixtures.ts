import type { PhaseName } from '@apex/pipeline-events'

export const TELECOM_DATASET_VERSION = 'telecom-perf-demo-3.0'
/** Owning project of the flagship ACC add-a-line certification. */
export const TELECOM_PROJECT_ID = 'acc-perf'
export const FLAGSHIP_APP_ID = 'app-acc-perf-acc'
export const FLAGSHIP_ENV_ID = 'env-acc-perf-acc-perfhyper2'
export const FLAGSHIP_ALIGN_QUERY =
  "programId eq 42 and itemtype in ('feature','story') and state ne 'Accepted' and contains(title,'ACC add-a-line')"

export const TELECOM_DATASET_REQUIREMENTS = {
  projects: 5,
  applications: 28,
  environments: 79,
  connections: 13,
  consumers: 4,
  documents: 8,
  evidencePackets: 12,
  jiraAlignItems: 19,
  initialJiraCloudItems: 8,
  savedQueries: 4,
  prompts: 84,
  seededRuns: 8,
  logs: 40,
  usageDays: 14,
  artifacts: 51,
} as const

export function deterministicAgentReply(phase: PhaseName, message: string): string {
  const lower = message.toLowerCase()
  if (/mq|queue|consumer/.test(lower)) {
    return 'The queue signal is isolated to IDP fulfillment on PERFHYPER2: IBM MQ depth rises after 200 concurrent add-a-line submits because only two consumers are active. The approved environment context calls for four consumers; I recommend restoring four and keeping the ramp hold at a depth of 10,000.'
  }
  if (/sla|release|risk/.test(lower)) {
    return 'The orchestration completed, but ACC submit p95 is 650ms above the 3,000ms release gate, and BSSe create-order accounts for 2,980ms of it. Treat this as not certified, link PERF-248, and retest on PERFHYPER2 after the BSSe and MQ actions are verified.'
  }
  if (/oracle|configuration|config|endpoint|target/.test(lower)) {
    return 'The PERFHYPER2 inventory shows mule-add-line-api routing IDP calls to idp.perfhyper3, a non-PERFHYPER2 IDP endpoint, so it would not reach the PERFHYPER2 path to PERF BSSe. I can apply the approved PERFHYPER2 mapping, rescan, and keep this finding in the audit trail.'
  }
  if (/tlg|batch|contention|shared/.test(lower)) {
    return 'PERF TLG and PERF BSSe are shared backends. Check the environment calendar and the T5 batch-driver schedule before resuming; overlapping load invalidates headroom results for every stack involved.'
  }
  return `For ${phase.replaceAll('_', ' ')}, I recorded your request and regenerated the deterministic result from the approved evidence packet and the selected application and environment context.`
}

export const ACC_DEMO_CONTEXT = `ACC is the browser-based application employees use to sell devices, cellular plans, and internet service. This test adds one line to an existing customer's cellular plan on PERFHYPER2: ACC calls Mule experience and process APIs, Mule calls IDP, and IDP creates the order in PERF BSSe and publishes the fulfillment event to IBM MQ for asynchronous activation. CRM records the interaction note.
An authorized employee signs in to ACC, finds a synthetic existing customer, opens the active plan, checks account eligibility and the plan line limit, selects Add a line, chooses a compatible phone or BYOD device and SIM, selects the line offer, reviews recurring and one-time charges, submits once, and verifies the order and new line in ACC and BSSe. Existing lines and plan entitlements must remain unchanged. Internet sales and device replacement are out of scope.
Assert one order, one new line, and one set of charges after a retry that reuses the idempotency key. Cover ineligible or suspended accounts, maximum line count, incompatible devices, unavailable SIM inventory, quote changes, expired employee sessions, concurrent submissions, Mule timeout/429/5xx, IDP or BSSe rejection, and delayed activation. Rejections must not leave a partial line or charge. Poll pending orders with bounded backoff and reconcile ambiguous outcomes before retrying.
All accounts, endpoints, infrastructure, traffic, thresholds, and results are synthetic demo assumptions, not verified environment configuration or measured production behavior. Use exclusive SYN- account leases, unique SIMs and request keys, reset added lines and test charges after each run, and verify cleanup. Event times are relative to demo activation.`

export const ACC_DEMO_TEST_PLAN = `# ACC add-a-line performance plan — PERFHYPER2 (ACC Perf)

${ACC_DEMO_CONTEXT}

## Topology
Frontend ACC (PERFHYPER2, primary system and traffic entry) > Mule (PERFHYPER2) > IDP (PERFHYPER2) > BSSe (PERF BSSe, backend). CRM (PERFHYPER2) is a middle-tier participant for interaction notes. PERF BSSe is shared with PERFHYPER1 and PERFHYPER3, so book it exclusively.

## Workload (illustrative, requires validation before a real run)
Run a one-employee smoke flow first. Ramp to 100, 200, and 300 concurrent employee sessions over 15 minutes, hold each step for 10 minutes, then drain pending activations. Model 5–15 seconds of employee think time between screens. Mix: lookup 30%, eligibility 20%, quote 25%, submit 20%, status 5%. The 85 TPS target is aggregate API traffic at Mule, not 85 completed line additions per second; report completed journeys separately. Keep the functional rejection matrix separate from the successful-journey error denominator. Compare against baselines 26.2.1 and 26.2.2.

## Checks and acceptance
Measure employee sign-in, account lookup, eligibility, offers, quote, submission, and confirmation separately, and correlate sessionId, accountId, quoteId, orderId, lineId, and the idempotency key across ACC, Mule, IDP, and BSSe. Gates: submit p95 ≤ 3,000ms, unexpected error rate < 2%, aggregate throughput ≥ 80 TPS. Zero duplicate orders, duplicate charges, unauthorized changes, or changes to existing lines are permitted. Report activation completion and outstanding orders separately from submission latency. Hold the ramp when IDP MQ depth exceeds 10,000 and drain before resuming. Abort on data corruption.

## Evidence and cleanup
Capture correlation IDs, per-tier timings (ACC, Mule, IDP, BSSe), final line counts, expected versus actual charges, rejection outcomes, and account and SIM lease records. Remove only this run's synthetic lines and charges, release inventory, verify the original plan and lines, and record any cleanup failures. Reports are simulated and cannot certify a real release.`

export const ACC_DEMO_TEST_DATA = `account_id,plan_id,account_status,current_lines,max_lines,device_id,sim_id,offer_id,expected_monthly_charge,expected_one_time_charge,expected_result
SYN-1001,PLAN-FAMILY-01,active,2,5,DEV-2001,SIM-3001,OFFER-LINE-01,25.00,10.00,one_new_line
SYN-1002,PLAN-FAMILY-01,active,5,5,DEV-2002,SIM-3002,OFFER-LINE-01,0.00,0.00,line_limit_rejected
SYN-1003,PLAN-FAMILY-01,suspended,2,5,DEV-2003,SIM-3003,OFFER-LINE-01,0.00,0.00,ineligible_account_rejected
SYN-1004,PLAN-FAMILY-01,active,1,5,DEV-INCOMPATIBLE,SIM-3004,OFFER-LINE-01,0.00,0.00,incompatible_device_rejected
`
