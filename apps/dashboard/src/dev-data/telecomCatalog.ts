// Offline demo catalog: projects, the applications they share (each registered once and
// linked to every project that tests it), environments, and the System / Phase /
// Application / Environment prompt layers that reference them.
// Application roles, hostnames, volumes and thresholds are synthetic demo assumptions.
import type { PhaseName } from '@apex/pipeline-events'

export type DemoLayer = 'frontend' | 'middle' | 'backend'
export type DemoAppCode = 'acc' | 'opus' | 'mule' | 'idp' | 'csi' | 'cas' | 'crm' | 'bobpm' | 'bsse' | 'tlg'
export type DemoEnvCode = 'perfhyper1' | 'perfhyper2' | 'perfhyper3' | 't3' | 't4' | 't5' | 'perf-tlg' | 'perf-bsse'

export interface DemoProject {
  id: string
  name: string
  apps: DemoAppCode[]
  focus: string
}

export interface DemoAppDefinition {
  code: DemoAppCode
  name: string
  layer: DemoLayer
  stack: 'perfhyper' | 't-stack' | 'shared'
  environments: DemoEnvCode[]
  description: string
  context: string
}

export interface DemoEnvironmentDefinition {
  code: DemoEnvCode
  name: string
  kind: 'k8s' | 'vm'
  stale: boolean
  context: string
}

export const DEMO_PROJECTS: DemoProject[] = [
  {
    id: 'acc-perf',
    name: 'ACC Perf',
    apps: ['acc', 'mule', 'idp', 'crm', 'bsse'],
    focus: 'ACC Perf certifies employee-assisted ACC sales and service journeys end to end on a PERFHYPER stack: ACC through Mule and IDP into BSSe, with CRM interaction reads. Release gates are per-journey p95 latency, unexpected error rate, completed journeys per hour, and order/charge integrity.',
  },
  {
    id: 'digital-perf',
    name: 'Digital Perf',
    apps: ['mule', 'idp', 'crm', 'bsse'],
    focus: 'Digital Perf tests the API tier consumed by digital self-service channels. There is no UI in scope: traffic enters at Mule experience APIs and flows through IDP into BSSe. Priorities are API p95/p99 latency, rate-limit headroom, connection-pool saturation, and multi-hour soak stability.',
  },
  {
    id: 'legacy-perf',
    name: 'Legacy Perf',
    apps: ['opus', 'csi', 'cas', 'bobpm', 'tlg'],
    focus: 'Legacy Perf certifies OPUS retail order entry on the T stacks: OPUS through CSI, CAS and BOBPM into TLG. Priorities are store-hour order throughput, CSI service latency, BOBPM workflow backlog, and the overlap between TLG nightly batch windows and online traffic.',
  },
  {
    id: 'npi',
    name: 'NPI',
    apps: ['acc', 'opus', 'mule', 'idp', 'csi', 'bsse', 'tlg'],
    focus: 'NPI runs launch-readiness tests for new products, rate plans and promotions. Each launch is tested on the stack that sells it (ACC to BSSe on PERFHYPER, or OPUS to TLG on the T stacks) with the launch-day traffic forecast, new catalog and offer data, and eligibility rules.',
  },
  {
    id: 'hrp',
    name: 'HRP',
    apps: ['acc', 'opus', 'mule', 'csi', 'crm', 'bsse', 'tlg'],
    focus: 'HRP runs cross-stack peak-readiness tests that combine the PERFHYPER and T-stack journeys against forecast peak volumes. Tests must coordinate shared-environment windows, especially PERF TLG and PERF BSSe, and report capacity headroom per tier.',
  },
]

export const DEMO_APPS: Record<DemoAppCode, DemoAppDefinition> = {
  acc: {
    code: 'acc', name: 'ACC', layer: 'frontend', stack: 'perfhyper', environments: ['perfhyper1', 'perfhyper2', 'perfhyper3'],
    description: 'Employee-facing web application used in retail and care to sell devices, rate plans and internet service. Calls Mule for account, eligibility, quote and order APIs.',
    context: 'ACC is a browser-based employee sales application hosted on the PERFHYPER stacks. Journeys: employee sign-in, customer/account search, eligibility, offer and device selection, quote, order submit, and order status. ACC calls Mule process APIs only; it never calls IDP or BSSe directly. Invariants: one order and one charge set per submit, idempotency key reused on retry, existing lines unchanged. Synthetic accounts are leased exclusively per virtual user and reset after each run. Telemetry: browser and API timings in Dynatrace, Mule correlation IDs propagated as x-correlation-id.',
  },
  opus: {
    code: 'opus', name: 'OPUS', layer: 'frontend', stack: 't-stack', environments: ['t3', 't4', 't5'],
    description: 'Retail store order-entry and point-of-sale client used for activations, upgrades and change-of-service. Submits through CSI to TLG.',
    context: 'OPUS is the retail store order-entry client deployed to the T stacks. Journeys: store-rep login, customer lookup, credit check (CAS), device/plan selection, upgrade or activation order, payment capture and receipt. OPUS calls CSI services; CSI fans out to CAS, BOBPM and TLG. Script traffic uses LoadRunner Web/HTTP with store and register IDs parameterized per virtual user. Invariants: one TLG service order per submit, no duplicate payment authorizations, and order fallout routed to BOBPM rather than retried blindly.',
  },
  mule: {
    code: 'mule', name: 'Mule', layer: 'middle', stack: 'perfhyper', environments: ['perfhyper1', 'perfhyper2', 'perfhyper3'],
    description: 'MuleSoft integration layer for ACC and digital channels: experience and process APIs for account, eligibility, quote and order calls routed to IDP.',
    context: 'Mule is the integration layer for ACC and digital channels on the PERFHYPER stacks. Experience APIs (account, eligibility, quote, order, order-status) call process APIs, which call IDP. Watch worker CPU, HTTP connection-pool utilization toward IDP, API Manager rate-limit policies (429s), and Object Store latency for idempotency keys. Mule must target the IDP instance on the same PERFHYPER stack; a cross-stack target invalidates results.',
  },
  idp: {
    code: 'idp', name: 'IDP', layer: 'middle', stack: 'perfhyper', environments: ['perfhyper1', 'perfhyper2', 'perfhyper3'],
    description: 'Integration and order-orchestration services between Mule and BSSe; the BSSe-side equivalent of CSI.',
    context: 'IDP is the order-orchestration and integration tier between Mule and BSSe, the BSSe-side equivalent of CSI. It validates orders, calls BSSe account, catalog and order APIs, and publishes fulfillment events to IBM MQ for asynchronous activation. Watch IDP thread pools, BSSe client timeouts and retries, MQ queue depth and oldest-message age, and the number of fulfillment consumers (approved: four per stack). Duplicate fulfillment events must be idempotent on orderId.',
  },
  csi: {
    code: 'csi', name: 'CSI', layer: 'middle', stack: 't-stack', environments: ['t3', 't4', 't5'],
    description: 'Service interface tier for OPUS: account, inventory, credit and order-submission services fronting TLG.',
    context: 'CSI is the service tier behind OPUS on the T stacks. It exposes SOAP/REST services for customer, inventory, credit (via CAS), order submission and order status, and writes service orders to TLG. Watch CSI JVM heap and GC pauses, WebLogic thread pools, TLG connection-pool waits, and SOAP fault rates per service. CSI calls PERF TLG, which is shared by T3, T4 and T5.',
  },
  cas: {
    code: 'cas', name: 'CAS', layer: 'middle', stack: 't-stack', environments: ['t3', 't4', 't5'],
    description: 'Credit and account-verification services called by CSI during OPUS activations and upgrades (demo role assumption).',
    context: 'CAS provides credit and account-verification decisions to CSI during OPUS activations and upgrades (demo role assumption). Its downstream credit bureau is stubbed in the T stacks with a fixed 400–900ms synthetic delay; tests must not count stub latency as a CAS defect. Watch decision latency by outcome (approve, deposit required, decline) and decision-cache hit rate.',
  },
  crm: {
    code: 'crm', name: 'CRM', layer: 'middle', stack: 'shared', environments: ['perfhyper1', 'perfhyper2', 'perfhyper3', 't3', 't4', 't5'],
    description: 'Customer interaction history and profile service read by ACC (through Mule) and OPUS (through CSI); deployed on both the PERFHYPER and T stacks.',
    context: 'CRM serves customer profile and interaction-history reads and writes an interaction note at order submit. It is deployed on both the PERFHYPER and T stacks and is called by Mule (ACC journeys) and CSI (OPUS journeys). Interaction-note writes are fire-and-forget and must not block order submission. Watch read p95, note-write queue lag, and search index refresh time.',
  },
  bobpm: {
    code: 'bobpm', name: 'BOBPM', layer: 'middle', stack: 't-stack', environments: ['t3', 't4', 't5'],
    description: 'Order workflow (BPM) service that runs asynchronous order steps and fallout handling for OPUS/CSI orders.',
    context: 'BOBPM runs asynchronous workflow steps for OPUS/CSI orders: provisioning hand-offs, fallout work queues and manual-review tasks. Tests measure workflow instance creation rate, step latency, open fallout tasks, and backlog drain time after load stops. Fallout tasks created by test data must be closed during cleanup.',
  },
  bsse: {
    code: 'bsse', name: 'BSSe', layer: 'backend', stack: 'perfhyper', environments: ['perf-bsse'],
    description: 'Billing and BSS backend for the PERFHYPER journeys: accounts, plans, line orders, activation and charges.',
    context: 'BSSe is the billing and BSS system of record for ACC and digital journeys. It owns accounts, plans and line limits, product catalog, orders, activation status and charges. All PERFHYPER stacks share the single PERF BSSe environment. Watch create-order and eligibility API latency, database CPU and lock waits, and the rating engine queue. Synthetic accounts are tagged SYN- and reset by the BSSe data-refresh job after each run.',
  },
  tlg: {
    code: 'tlg', name: 'TLG', layer: 'backend', stack: 't-stack', environments: ['perf-tlg'],
    description: 'Legacy billing and provisioning backend for OPUS/CSI orders and legacy-billed accounts; shared by the T stacks.',
    context: 'TLG is the legacy billing and provisioning system of record for OPUS/CSI orders and for legacy-billed accounts reached from other journeys. All T stacks share one PERF TLG environment, so concurrent T3/T4/T5 tests contend for the same database and transaction monitor. Nightly batch (bill cycle and usage rating) runs 01:00–04:00 CT and must not overlap a certification window. Watch transaction-monitor queue depth, DB2 lock waits, and service-order commit latency.',
  },
}

export const DEMO_ENVIRONMENTS: Record<DemoEnvCode, DemoEnvironmentDefinition> = {
  perfhyper1: {
    code: 'perfhyper1', name: 'PERFHYPER1', kind: 'k8s', stale: true,
    context: 'PERFHYPER1 is the AKS performance stack hosting ACC, Mule, IDP and CRM. It is sized at about 50% of production and runs the previous release train, so use it for soak and regression baselines rather than release certification. It calls PERF BSSe and, for legacy-billed accounts, PERF TLG.',
  },
  perfhyper2: {
    code: 'perfhyper2', name: 'PERFHYPER2', kind: 'k8s', stale: false,
    context: 'PERFHYPER2 is the production-parity AKS stack for release certification. It hosts ACC, Mule, IDP and CRM on the current release candidate, with HPA enabled (ACC 4–8 pods, Mule 3–6 workers, IDP 3–6 pods) and four IBM MQ fulfillment consumers approved. Downstream targets: IDP on PERFHYPER2 only, PERF BSSe for billing, PERF TLG for legacy-billed account lookups. Book through the PERFHYPER calendar; PERF BSSe is shared with PERFHYPER1 and PERFHYPER3.',
  },
  perfhyper3: {
    code: 'perfhyper3', name: 'PERFHYPER3', kind: 'k8s', stale: false,
    context: 'PERFHYPER3 is the AKS stack used for NPI and feature-branch builds of ACC, Mule, IDP and CRM. It is sized like PERFHYPER2 but may carry unreleased catalog and offer configuration, so confirm the build and catalog version before comparing it with PERFHYPER2 baselines. It shares PERF BSSe with the other PERFHYPER stacks.',
  },
  t3: {
    code: 't3', name: 'T3', kind: 'vm', stale: false,
    context: 'T3 is a T-stack performance environment hosting OPUS, CSI, CAS, BOBPM and CRM on VMs (WebLogic). It is the HRP peak-readiness stack. It writes to the shared PERF TLG environment, so coordinate windows with T4 and T5 and avoid the TLG nightly batch (01:00–04:00 CT).',
  },
  t4: {
    code: 't4', name: 'T4', kind: 'vm', stale: false,
    context: 'T4 is the production-parity T stack for Legacy Perf release certification of OPUS, CSI, CAS, BOBPM and CRM. Store-hour peak is modeled at 1,800 concurrent store reps. It writes to shared PERF TLG; book PERF TLG exclusively for certification runs. The CAS credit-bureau stub adds a 400–900ms synthetic delay.',
  },
  t5: {
    code: 't5', name: 'T5', kind: 'vm', stale: true,
    context: 'T5 is the T stack used for NPI and patch validation. It currently runs one patch level behind T4 and its inventory snapshot is stale. It shares PERF TLG with T3 and T4. It also hosts the TLG batch-driver jobs, so check that no batch job is scheduled before starting online load.',
  },
  'perf-tlg': {
    code: 'perf-tlg', name: 'PERF TLG', kind: 'vm', stale: false,
    context: 'PERF TLG is the single legacy billing and provisioning environment shared by T3, T4 and T5, and by PERFHYPER journeys that read legacy-billed accounts. It has a DB2 database, a transaction monitor, and nightly bill-cycle batch from 01:00 to 04:00 CT. Only one certification-grade load may run at a time. Lock waits and transaction-monitor queue depth are the primary saturation signals.',
  },
  'perf-bsse': {
    code: 'perf-bsse', name: 'PERF BSSe', kind: 'k8s', stale: false,
    context: 'PERF BSSe is the shared billing/BSS environment for all PERFHYPER stacks. It is sized at about 60% of production for order APIs and rating. Concurrent tests from more than one PERFHYPER stack must be booked as a combined window. Synthetic SYN- accounts are refreshed nightly; tests must not create real-format account numbers.',
  },
}

/** The project that first registered an application; its id keeps that project's prefix. */
export const demoHomeProject = (app: DemoAppCode): string => DEMO_PROJECTS.find(project => project.apps.includes(app))!.id
/** Projects linked to an application (every project that tests it). */
export const demoAppProjects = (app: DemoAppCode): string[] => DEMO_PROJECTS.filter(project => project.apps.includes(app)).map(project => project.id)
/** Applications are shared across projects, so the id ignores which project asks. */
export const demoAppId = (_project: string, app: DemoAppCode): string => `app-${demoHomeProject(app)}-${app}`
export const demoEnvId = (_project: string, app: DemoAppCode, env: DemoEnvCode): string => `env-${demoHomeProject(app)}-${app}-${env}`
export const demoBaseUrl = (app: DemoAppCode, env: DemoEnvCode): string =>
  env === 'perf-tlg' || env === 'perf-bsse' ? `https://${app}.${env}.example.test` : `https://${app}.${env}.perf.example.test`

/** Downstream targets each tier is approved to call in a given environment. */
export function demoDependencies(app: DemoAppCode, env: DemoEnvCode): Array<{ application: string; environment: string; base_url: string; purpose: string }> {
  const dep = (target: DemoAppCode, targetEnv: DemoEnvCode, purpose: string) => ({ application: DEMO_APPS[target].name, environment: DEMO_ENVIRONMENTS[targetEnv].name, base_url: demoBaseUrl(target, targetEnv), purpose })
  if (app === 'acc') return [dep('mule', env, 'Account, eligibility, quote, order and order-status APIs'), dep('crm', env, 'Customer profile and interaction history through Mule')]
  if (app === 'mule') return [dep('idp', env, 'Order orchestration, eligibility and catalog process calls'), dep('crm', env, 'Interaction-note writes'), dep('tlg', 'perf-tlg', 'Legacy-billed account lookups')]
  if (app === 'idp') return [dep('bsse', 'perf-bsse', 'Account, catalog, create-order and activation APIs'), { application: 'IBM MQ', environment: DEMO_ENVIRONMENTS[env].name, base_url: `mq://idp-fulfillment.${env}.perf.example.test`, purpose: 'Asynchronous fulfillment events (four consumers approved)' }]
  if (app === 'opus') return [dep('csi', env, 'Customer, credit, inventory and order-submission services')]
  if (app === 'csi') return [dep('cas', env, 'Credit and account verification'), dep('bobpm', env, 'Order workflow and fallout'), dep('crm', env, 'Customer profile and interaction notes'), dep('tlg', 'perf-tlg', 'Service-order commit and account master')]
  if (app === 'bobpm') return [dep('tlg', 'perf-tlg', 'Provisioning and service-order status updates')]
  if (app === 'crm' && env.startsWith('t')) return [dep('tlg', 'perf-tlg', 'Legacy account profile reads')]
  return []
}

// ---------------------------------------------------------------------------
// Prompt layers
// ---------------------------------------------------------------------------

export const DEMO_SYSTEM_PROMPTS = [
  {
    id: 'prompt-system-telecom-coe',
    key: 'telecom-perf-coe',
    description: 'Telecom Performance Engineering CoE standard',
    is_default: true,
    content: `You support the Telecom Performance Engineering Center of Excellence. Every test runs against shared, non-production performance environments (PERFHYPER1-3, T3-T5, PERF BSSe, PERF TLG) using synthetic data only.
Work only from the selected phase instructions, application and environment context, linked work items, and supplied evidence. Treat hostnames, prior runs, and operator statements as claims to check against approved inventory and telemetry.
Use the participant labels in the test scope (frontend, middle tier, backend; primary system; traffic entry) when you describe systems. Report latency as p95/p99 over a stated steady-state window, throughput in the unit the plan defines (TPS, orders/hour, or concurrent sessions), and errors with their denominator.
Never invent thresholds, approvals, owners, ticket numbers, or results. Mark missing inputs UNKNOWN and state which phase they block. Protect shared environments: hold or stop load when a hard-stop condition is reached and call out contention with other stacks on PERF BSSe or PERF TLG.`,
  },
  {
    id: 'prompt-system-release-cert',
    key: 'release-certification',
    description: 'Release certification (strict evidence, go/no-go)',
    is_default: false,
    content: `This test is a release-certification run whose output feeds a go/no-go decision. Apply the Telecom Performance Engineering CoE standard with these additional rules:
Compare results only with the last certified baseline on the same environment, build train, and workload model. Name the baseline run explicitly.
Every acceptance criterion must resolve to pass, fail, or unknown, with an evidence reference. Unknown is never reported as pass.
Do not recommend "certified with conditions" unless each condition has a linked defect or an accepted-risk record supplied in the evidence.
Keep the executive summary to five sentences or fewer and put all qualifications in the technical findings.`,
  },
]

export interface DemoPhasePromptSeed {
  id: string
  phase: PhaseName
  key: string
  description: string
  content: string
}

export const DEMO_PHASE_ALTERNATIVES: DemoPhasePromptSeed[] = [
  {
    id: 'prompt-phase-alt-story', phase: 'story_analysis', key: 'story_analysis/telecom-order-journeys',
    description: 'Telecom order journeys: decompose by tier and order lifecycle',
    content: `Decompose the supplied Jira Align features and stories into performance scenarios along the telecom order lifecycle: lookup, eligibility/credit, offer and quote, submit, asynchronous fulfillment, and status.
For each scenario list the participating tiers in call order (for example ACC > Mule > IDP > BSSe, or OPUS > CSI > CAS/BOBPM > TLG), the synchronous boundary the user waits on, and any asynchronous tail (IBM MQ events, BOBPM workflow) that completes later.
Flag idempotency and duplicate-order risks, retry paths, and shared backends (PERF BSSe, PERF TLG). Map every scenario to its story key; list stories with no performance-relevant behavior as excluded, with a reason.`,
  },
  {
    id: 'prompt-phase-alt-plan-peak', phase: 'test_planning', key: 'test_planning/stepped-peak-ramp',
    description: 'Stepped peak ramp with shared-backend hold points',
    content: `Plan a stepped ramp: smoke, then 33%, 66% and 100% of the forecast peak, each step held for at least 10 minutes of steady state, followed by a drain phase for asynchronous work.
Define hold points that pause the ramp when a shared-backend signal (PERF BSSe create-order p95, PERF TLG lock waits, IBM MQ depth or oldest-message age) breaches its warning level, and hard stops for data-integrity failures.
State the workload mix as journey shares totaling 100%, with think time and pacing per journey. Report completed business journeys separately from API TPS. Name the baseline runs used for comparison and confirm they ran on the same environment and build train.`,
  },
  {
    id: 'prompt-phase-alt-plan-soak', phase: 'test_planning', key: 'test_planning/api-soak',
    description: 'API soak for headless Digital Perf tests',
    content: `Plan a headless API soak at the steady-state arrival rate from the supplied digital traffic forecast (open model, requests per second per API), for 4–8 hours.
Include a 15-minute warm-up excluded from measurement, hourly checkpoint windows for p95/p99 latency, error rate, and resource trend, and leak indicators (heap after GC, connection-pool growth, Object Store size).
Define rate-limit (429) handling: count 429s separately and do not retry them inside the measured transaction. State the abort conditions and the shared PERF BSSe window booked for the soak.`,
  },
  {
    id: 'prompt-phase-alt-env', phase: 'env_triage', key: 'env_triage/shared-stack-contention',
    description: 'Readiness with shared PERF BSSe / PERF TLG contention checks',
    content: `In addition to the standard readiness checks, verify that each tier calls the downstream instance approved for this environment: Mule > IDP on the same PERFHYPER stack, CSI > CAS/BOBPM on the same T stack, and every backend call to PERF BSSe or PERF TLG.
Check the environment calendar and live telemetry for concurrent load from other stacks on PERF BSSe or PERF TLG, and for TLG nightly batch overlap (01:00–04:00 CT).
Confirm replica counts and consumer counts against the approved environment context (for example four IDP fulfillment consumers), and report stale inventory snapshots as not_checked.`,
  },
  {
    id: 'prompt-phase-alt-script', phase: 'script_scenario', key: 'script_scenario/loadrunner-web-http',
    description: 'LoadRunner Web/HTTP scripts for OPUS store journeys',
    content: `Produce LoadRunner Web/HTTP (VuGen C) script drafts for OPUS store journeys, one Action per journey step with lr_start_transaction/lr_end_transaction names that match the plan's transaction IDs.
Correlate the OPUS session token, CSI order ID, and TLG service-order number with web_reg_save_param_ex before use. Parameterize store ID, register ID, rep login, and synthetic customer from unique-per-iteration data files.
Add text checks for CSI SOAP faults and OPUS error banners. Keep think time in lr_think_time so the controller can scale it. Do not hard-code environment hosts; use the T-stack runtime parameter file.`,
  },
  {
    id: 'prompt-phase-alt-report', phase: 'reporting', key: 'reporting/release-certification',
    description: 'Release certification report with per-tier attribution',
    content: `Write the certification report in two parts. Executive: overall result (certified, not certified, or certified with linked conditions), the top three risks, and the go/no-go recommendation. Technical: a per-journey table (measured value, target, window, verdict, evidence) and a per-tier attribution of latency and errors for frontend, middle tier and backend.
Attribute a bottleneck to a tier only when its telemetry supports it. Compare against the named baseline. For each failed criterion draft a Jira defect in project PERF with the affected tier, environment, metric evidence, and a reproduction window.`,
  },
  {
    id: 'prompt-phase-alt-postmortem', phase: 'postmortem', key: 'postmortem/blameless-actions',
    description: 'Blameless review with environment-ownership actions',
    content: `Run a blameless review. Build the timeline from execution events, gate decisions, and telemetry annotations. Separate environment issues (misrouted targets, consumer counts, shared-backend contention, batch overlap) from application defects.
For each action give an action ID, the finding it addresses, a proposed owning role (environment team, application team, performance engineering), a verification step, and the retest it unblocks. Do not assign named people.`,
  },
]

export interface DemoContextPromptSeed {
  id: string
  key: string
  description: string
  content: string
  application_id?: string | null
  environment_id?: string | null
  is_shared?: boolean
}

export function demoApplicationPrompts(): DemoContextPromptSeed[] {
  const rows: DemoContextPromptSeed[] = []
  // One context per application: it describes the system, whichever project tests it.
  const codes = [...new Set(DEMO_PROJECTS.flatMap(project => project.apps))]
  for (const code of codes) {
    const app = DEMO_APPS[code]
    const id = demoAppId('', code)
    const projects = DEMO_PROJECTS.filter(project => project.apps.includes(code)).map(project => project.name)
    rows.push({
      id: `prompt-${id}`,
      key: id,
      application_id: id,
      description: `${app.name} application context`,
      content: `${app.name} (${app.layer === 'middle' ? 'middle tier' : app.layer})\n${app.context}\n\nTested by: ${projects.join(', ')}`,
    })
  }
  rows.push({
    id: 'prompt-shared-order-integrity',
    key: 'order-integrity',
    application_id: null,
    is_shared: true,
    description: 'Shared order-integrity invariants (all order journeys)',
    content: 'Across every order journey: one submit creates exactly one order, one set of charges, and one fulfillment workflow, even when the client retries with the same idempotency key. Rejected orders leave no partial line, charge, or open fallout task. After the run, reconcile order counts between the entry tier and the system of record (BSSe or TLG) and report any mismatch as a data-integrity failure.',
  })
  return rows
}

/** Environment Context is authored for traffic-entry and system-of-record registrations. */
export function demoEnvironmentPrompts(): DemoContextPromptSeed[] {
  const rows: DemoContextPromptSeed[] = []
  for (const project of DEMO_PROJECTS) {
    const anchors = project.apps.filter((code) => DEMO_APPS[code].layer !== 'middle' || (code === 'mule' && !project.apps.some((item) => DEMO_APPS[item].layer === 'frontend')))
    for (const code of anchors) {
      for (const env of DEMO_APPS[code].environments) {
        const id = demoEnvId(project.id, code, env)
        // Environments belong to their (shared) application: describe each once.
        if (rows.some(row => row.id === `prompt-${id}`)) continue
        const environment = DEMO_ENVIRONMENTS[env]
        rows.push({
          id: `prompt-${id}`,
          key: id,
          environment_id: id,
          description: `${environment.name} environment context — ${DEMO_APPS[code].name}`,
          content: `${environment.name} for ${DEMO_APPS[code].name}\n${environment.context}`,
        })
      }
    }
  }
  return rows
}

// ---------------------------------------------------------------------------
// Seeded run narratives
// ---------------------------------------------------------------------------

export interface DemoScopeParticipantSeed {
  id: string
  app: DemoAppCode
  env: DemoEnvCode
}

export interface DemoRunProfile {
  key: 'acc-flagship' | 'acc-baseline' | 'npi-launch' | 'legacy-opus' | 'hrp-contention' | 'digital-soak' | 'legacy-baseline' | 'digital-baseline' | 'hrp-baseline'
  project: string
  participants: DemoScopeParticipantSeed[]
  primary: string
  traffic: string | null
  request: string
  workItems: string[]
  stories: string
  plan: string
  planWarnings: string[]
  envSummary: string
  envWarnings: string[]
  script: string
  scriptRefs: string[]
  vusers: number
  rampS: number
  durationS: number
  slas: { p95_ms: number; error_rate: number }
  executionSummary: string
  executionWarnings: string[]
  kpis: { tps_avg: number; p95_ms: number; error_rate: number; vusers_peak: number }
  passed: boolean
  breaches: string[]
  executionNotes: string
  reporting: string
  postmortem: string
  dialogue: Array<{ phase: PhaseName; operator: string; agent: string }>
  contextPacket: { source: string; title: string; summary: string; ref: string }
}

const scope = (entries: Array<[DemoAppCode, DemoEnvCode]>): DemoScopeParticipantSeed[] =>
  entries.map(([app, env]) => ({ id: `${app}-${env}`, app, env }))

export const DEMO_RUN_PROFILES: Record<DemoRunProfile['key'], DemoRunProfile> = {
  'acc-flagship': {
    key: 'acc-flagship',
    project: 'acc-perf',
    participants: scope([['acc', 'perfhyper2'], ['mule', 'perfhyper2'], ['idp', 'perfhyper2'], ['crm', 'perfhyper2'], ['bsse', 'perf-bsse']]),
    primary: 'acc-perfhyper2',
    traffic: 'acc-perfhyper2',
    request: '',
    workItems: ['feature:583', 'story:9012', 'story:9013'],
    stories: 'Mapped feature:583, story:9012 and story:9013 to five scenarios: S1 account lookup and eligibility, S2 offer and quote, S3 add-a-line submit (ACC > Mule > IDP > BSSe), S4 asynchronous activation via IBM MQ, and S5 idempotent retry. Line-limit and suspended-account rejections are a separate functional matrix. Internet sales are excluded.',
    plan: 'Stepped ramp to 300 concurrent ACC employee sessions (100/200/300, 10-minute holds), 5–15s think time, 85 aggregate API TPS at peak. Mix: lookup 30%, eligibility 20%, quote 25%, submit 20%, status 5%. Gates: submit p95 ≤ 3,000ms, unexpected errors < 2%, zero duplicate orders or charges. Hold the ramp if IDP MQ depth exceeds 10,000.',
    planWarnings: ['BSSe create-order p95 in baseline 26.2.2 was 2,520ms, within 20% of the 3,000ms gate.'],
    envSummary: 'PERFHYPER2 is reachable and on build ACC 26.3.0-rc4. Two findings: mule-add-line-api on PERFHYPER2 routes IDP calls to idp.perfhyper3 (a non-PERFHYPER2 endpoint), and only two of the four approved IDP fulfillment MQ consumers are running.',
    envWarnings: ['Mule targets a non-PERFHYPER2 IDP endpoint; results would include PERFHYPER3 latency until corrected.', 'IDP fulfillment consumers: 2 running, 4 approved.'],
    script: 'Generated a correlated JMeter plan (acc-add-line.jmx) with transactions T01–T07 matching the plan, x-correlation-id and idempotency-key headers, synthetic SYN- account leases, and SLA assertions. Validation: syntax passed, correlation passed on a one-user smoke run.',
    scriptRefs: ['memory://scripts/acc-add-line.jmx'],
    vusers: 300,
    rampS: 900,
    durationS: 1_500,
    slas: { p95_ms: 3_000, error_rate: 0.02 },
    executionSummary: 'Reached 300 sessions and 84.7 TPS. Submit p95 was 3,650ms against the 3,000ms gate, driven by BSSe create-order. IDP MQ depth peaked at 18,420 and drained in 6 minutes after a ramp hold. AKS HPA scaled ACC from 4 to 8 pods.',
    executionWarnings: ['BSSe create-order p95 exceeded the 3,000ms gate at the 300-session step.'],
    kpis: { tps_avg: 82.7, p95_ms: 3_650, error_rate: 0.028, vusers_peak: 300 },
    passed: false,
    breaches: ['acc_submit_p95_ms', 'error_rate'],
    executionNotes: 'The orchestration completed; the release candidate fails the submit p95 and error-rate gates pending BSSe and MQ remediation.',
    reporting: 'Not certified: submit p95 3,650ms vs 3,000ms (fail), errors 2.8% vs 2% (fail), throughput 84.7 vs 80 TPS (pass), zero duplicate orders (pass). Bottleneck attributed to BSSe create-order (p95 2,980ms of the 3,650ms) with IDP MQ backlog as a contributor. Draft defect PERF-248.',
    postmortem: 'Actions: scale IDP fulfillment consumers to four (environment team), correct the Mule IDP target and add an env-triage guard (performance engineering), optimize the BSSe create-order path (BSSe team). Retest on PERFHYPER2 after all three are verified.',
    dialogue: [
      { phase: 'test_planning', operator: 'Include asynchronous MQ backlog and retry-idempotency coverage.', agent: 'Added S4 (activation via IDP MQ) with a depth hold point at 10,000, and S5 (retry with the same idempotency key) at 2% of submits.' },
    ],
    contextPacket: { source: 'jira_align', title: 'story:9012 acceptance criteria', summary: 'ACC add-a-line must stay idempotent while IDP fulfillment traverses IBM MQ to BSSe.', ref: 'story:9012' },
  },
  'acc-baseline': {
    key: 'acc-baseline',
    project: 'acc-perf',
    participants: scope([['acc', 'perfhyper2'], ['mule', 'perfhyper2'], ['idp', 'perfhyper2'], ['bsse', 'perf-bsse']]),
    primary: 'acc-perfhyper2',
    traffic: 'acc-perfhyper2',
    request: 'Re-run the certified ACC add-a-line workload on PERFHYPER2 to refresh the release baseline. Same 300-session stepped ramp and journey mix as the prior certification.',
    workItems: ['feature:583'],
    stories: 'Reused the certified scenario set S1–S5 from feature:583; no new stories in scope.',
    plan: 'Unchanged baseline workload: 300 sessions, 85 TPS target, 25-minute measured window. Gates: submit p95 ≤ 3,000ms, errors < 2%.',
    planWarnings: [],
    envSummary: 'PERFHYPER2 ready: all targets match approved inventory, four IDP fulfillment consumers running, PERF BSSe booked exclusively.',
    envWarnings: [],
    script: 'Reused the validated acc-add-line.jmx from the previous certification without changes.',
    scriptRefs: ['memory://scripts/acc-add-line.jmx'],
    vusers: 300,
    rampS: 900,
    durationS: 1_500,
    slas: { p95_ms: 3_000, error_rate: 0.02 },
    executionSummary: 'Baseline passed: steady 300 sessions, submit p95 within gate, MQ depth peaked below 2,000.',
    executionWarnings: [],
    kpis: { tps_avg: 81.2, p95_ms: 2_410, error_rate: 0.006, vusers_peak: 300 },
    passed: true,
    breaches: [],
    executionNotes: 'Comparable ACC add-a-line baseline passed all release gates.',
    reporting: 'Baseline accepted: all gates passed. Stored as the comparison baseline for the next release candidate.',
    postmortem: 'No follow-up actions. Baseline recorded.',
    dialogue: [],
    contextPacket: { source: 'documents', title: 'historical-baselines.json', summary: 'Prior certified PERFHYPER2 runs for ACC add-a-line.', ref: 'doc-baselines' },
  },
  'npi-launch': {
    key: 'npi-launch',
    project: 'npi',
    participants: scope([['acc', 'perfhyper3'], ['mule', 'perfhyper3'], ['idp', 'perfhyper3'], ['bsse', 'perf-bsse']]),
    primary: 'acc-perfhyper3',
    traffic: 'acc-perfhyper3',
    request: 'Launch-readiness rehearsal for the new Unlimited Premium plan on PERFHYPER3. Model launch-day traffic: 2.5x normal plan-change and new-line volume through ACC for the first 4 hours, with new catalog and offer data loaded in BSSe.',
    workItems: ['feature:612', 'story:9120'],
    stories: 'Three launch scenarios: L1 plan-change to Unlimited Premium on existing accounts, L2 new line with the launch promo, L3 eligibility for the launch offer (catalog lookup in BSSe). Each follows ACC > Mule > IDP > BSSe; activation is asynchronous through IDP MQ.',
    plan: 'Ramp to 2.5x forecast (375 sessions) in 3 steps, 15-minute holds, then a 60-minute launch-day hold. Gates: plan-change p95 ≤ 2,500ms, new-line submit p95 ≤ 3,000ms, catalog lookup p95 ≤ 800ms, errors < 1%.',
    planWarnings: ['Launch traffic forecast is provisional (marketing estimate v2); confirm before certification.'],
    envSummary: 'PERFHYPER3 ready on ACC 26.4.0-npi2 with the Unlimited Premium catalog loaded in PERF BSSe. PERF BSSe is shared with a PERFHYPER1 regression window ending 30 minutes into this test.',
    envWarnings: ['PERF BSSe overlaps a PERFHYPER1 regression window for the first 30 minutes.'],
    script: 'Extended the ACC JMeter plan with L1–L3 transactions, launch promo codes, and catalog-version assertions.',
    scriptRefs: ['memory://scripts/acc-npi-unlimited-premium.jmx'],
    vusers: 375,
    rampS: 1_800,
    durationS: 3_600,
    slas: { p95_ms: 3_000, error_rate: 0.01 },
    executionSummary: 'Held 375 sessions for 60 minutes. BSSe catalog lookup p95 rose to 1,340ms (gate 800ms) while PERF BSSe was shared; IDP MQ depth reached 9,800 and recovered. New-line submit p95 2,870ms passed.',
    executionWarnings: ['BSSe catalog lookup p95 1,340ms exceeded the 800ms gate during the shared PERF BSSe window.'],
    kpis: { tps_avg: 104.6, p95_ms: 2_870, error_rate: 0.012, vusers_peak: 375 },
    passed: false,
    breaches: ['bsse_catalog_lookup_p95_ms', 'error_rate'],
    executionNotes: 'Launch rehearsal completed; catalog lookup and error-rate gates failed. Part of the catalog latency overlaps the shared PERF BSSe window and needs an isolated retest.',
    reporting: 'Conditional: plan-change and new-line gates passed; BSSe catalog lookup failed (1,340ms vs 800ms) and errors 1.2% vs 1%. Contention with the PERFHYPER1 window is a confounder. Recommend an isolated PERF BSSe retest before the launch decision.',
    postmortem: 'Actions: book PERF BSSe exclusively for NPI retests (performance engineering), add a catalog cache warm-up after catalog load (BSSe team), and re-confirm the launch traffic forecast (NPI program).',
    dialogue: [
      { phase: 'reporting', operator: 'How much of the catalog latency is from the shared window?', agent: 'In the first 30 minutes (shared) catalog p95 was 1,340ms; after the PERFHYPER1 window ended it fell to 910ms, still above the 800ms gate. Contention explains part of the breach, not all of it.' },
    ],
    contextPacket: { source: 'jira_align', title: 'feature:612 launch acceptance', summary: 'Unlimited Premium launch: plan change and new line must meet launch-day volumes on PERFHYPER3.', ref: 'feature:612' },
  },
  'legacy-opus': {
    key: 'legacy-opus',
    project: 'legacy-perf',
    participants: scope([['opus', 't4'], ['csi', 't4'], ['cas', 't4'], ['bobpm', 't4'], ['tlg', 'perf-tlg']]),
    primary: 'opus-t4',
    traffic: 'opus-t4',
    request: 'Certify OPUS device-upgrade and activation orders for PI 26.4 on T4 at store-hour peak (1,800 concurrent store reps), including CAS credit decisions and BOBPM fallout handling into PERF TLG.',
    workItems: ['feature:705', 'story:9301', 'story:9302'],
    stories: 'Four scenarios: O1 rep login and customer lookup, O2 device upgrade (OPUS > CSI > TLG), O3 new activation with CAS credit check, O4 order fallout routed to BOBPM. TLG nightly batch overlap is excluded by scheduling.',
    plan: 'Closed model with 1,800 store reps, 20–40s think time, 30-minute steady state. Mix: lookup 35%, upgrade 40%, activation 20%, status 5%. Gates: upgrade submit p95 ≤ 4,000ms, CSI fault rate < 0.5%, BOBPM backlog drains within 15 minutes.',
    planWarnings: ['Awaiting operator review: the CAS credit-bureau stub delay (400–900ms) must be excluded from the CAS latency gate.'],
    envSummary: 'Not started.',
    envWarnings: [],
    script: 'Not started.',
    scriptRefs: ['memory://scripts/opus_upgrade.c'],
    vusers: 1_800,
    rampS: 1_200,
    durationS: 1_800,
    slas: { p95_ms: 4_000, error_rate: 0.005 },
    executionSummary: 'Not started.',
    executionWarnings: [],
    kpis: { tps_avg: 0, p95_ms: 0, error_rate: 0, vusers_peak: 0 },
    passed: false,
    breaches: [],
    executionNotes: '',
    reporting: '',
    postmortem: '',
    dialogue: [
      { phase: 'test_planning', operator: 'Make sure the TLG nightly batch is out of the window.', agent: 'Scheduled the steady state for 14:00–14:30 CT and added a pre-check that blocks the start if any TLG batch job is active on PERF TLG.' },
    ],
    contextPacket: { source: 'jira_align', title: 'feature:705 store upgrade peak', summary: 'OPUS upgrade orders must sustain store-hour peak on T4 with PERF TLG booked exclusively.', ref: 'feature:705' },
  },
  'hrp-contention': {
    key: 'hrp-contention',
    project: 'hrp',
    participants: scope([['opus', 't3'], ['csi', 't3'], ['crm', 't3'], ['tlg', 'perf-tlg']]),
    primary: 'opus-t3',
    traffic: 'opus-t3',
    request: 'HRP peak-readiness: drive OPUS activations and upgrades on T3 at 120% of forecast peak to measure PERF TLG headroom for the holiday window.',
    workItems: ['objective:88'],
    stories: 'Two scenarios: H1 OPUS upgrade at 120% of forecast, H2 activation with CRM interaction notes. Both commit service orders to PERF TLG.',
    plan: 'Ramp to 2,160 store reps (120% of 1,800) in four steps. Hard stops: PERF TLG lock-wait time > 2s sustained 3 minutes, or CSI fault rate > 5%.',
    planWarnings: [],
    envSummary: 'T3 ready. The PERF TLG calendar shows no other booking, but the inventory scan found the T5 batch driver configured to start a usage-rating job at 14:20 CT.',
    envWarnings: ['T5 batch driver will run against PERF TLG during this window.'],
    script: 'Reused the validated OPUS LoadRunner scripts (opus_upgrade.c, opus_activation.c) with T3 runtime parameters.',
    scriptRefs: ['memory://scripts/opus_upgrade.c', 'memory://scripts/opus_activation.c'],
    vusers: 2_160,
    rampS: 1_200,
    durationS: 2_400,
    slas: { p95_ms: 4_000, error_rate: 0.01 },
    executionSummary: 'Aborted at the 1,620-rep step: the T5 usage-rating batch started on PERF TLG, lock waits reached 4.8s and the CSI fault rate rose to 7%, triggering the hard stop. Results are not usable for headroom.',
    executionWarnings: ['Hard stop: PERF TLG lock waits 4.8s, CSI faults 7% during T5 batch overlap.'],
    kpis: { tps_avg: 41.3, p95_ms: 9_820, error_rate: 0.07, vusers_peak: 1_620 },
    passed: false,
    breaches: ['tlg_lock_wait_s', 'csi_fault_rate'],
    executionNotes: 'Aborted by hard stop; shared PERF TLG contention invalidates the run.',
    reporting: '',
    postmortem: '',
    dialogue: [],
    contextPacket: { source: 'jira_align', title: 'objective:88 holiday peak readiness', summary: 'Demonstrate 20% headroom on PERF TLG above forecast holiday peak.', ref: 'objective:88' },
  },
  'digital-soak': {
    key: 'digital-soak',
    project: 'digital-perf',
    participants: scope([['mule', 'perfhyper1'], ['idp', 'perfhyper1'], ['bsse', 'perf-bsse']]),
    primary: 'mule-perfhyper1',
    traffic: 'mule-perfhyper1',
    request: 'Six-hour soak of the Mule order-status and account-summary experience APIs used by digital channels, on PERFHYPER1 at the steady-state forecast of 140 requests/second.',
    workItems: ['feature:640'],
    stories: 'Two API scenarios: D1 order-status (Mule > IDP > BSSe), D2 account summary (Mule > IDP > BSSe, cache-eligible). No UI in scope.',
    plan: 'Open model at 140 req/s (D1 60%, D2 40%) for 6 hours after a 15-minute warm-up. Gates: p95 ≤ 600ms, p99 ≤ 1,500ms, errors < 0.5%, no upward trend in Mule heap after GC.',
    planWarnings: [],
    envSummary: 'PERFHYPER1 ready on build train 26.2 (stale inventory snapshot refreshed). PERF BSSe booked for the soak window.',
    envWarnings: ['PERFHYPER1 runs the previous release train; results are a regression baseline, not a certification.'],
    script: 'Generated a JMeter API plan with constant-throughput timers, OAuth token refresh every 50 minutes, and per-API assertions.',
    scriptRefs: ['memory://scripts/mule-digital-soak.jmx'],
    vusers: 120,
    rampS: 900,
    durationS: 21_600,
    slas: { p95_ms: 600, error_rate: 0.005 },
    executionSummary: 'APEX Load reservation timed out after remote acceptance; awaiting reconciliation retry.',
    executionWarnings: ['Provisioning reconciliation required before the soak can start.'],
    kpis: { tps_avg: 0, p95_ms: 0, error_rate: 0, vusers_peak: 0 },
    passed: false,
    breaches: [],
    executionNotes: 'Provisioning reconciliation pending.',
    reporting: '',
    postmortem: '',
    dialogue: [],
    contextPacket: { source: 'jira_align', title: 'feature:640 digital order status', summary: 'Order-status API must sustain digital-channel load with p95 ≤ 600ms.', ref: 'feature:640' },
  },
  'legacy-baseline': {
    key: 'legacy-baseline',
    project: 'legacy-perf',
    participants: scope([['opus', 't4'], ['csi', 't4'], ['cas', 't4'], ['bobpm', 't4'], ['tlg', 'perf-tlg']]),
    primary: 'opus-t4',
    traffic: 'opus-t4',
    request: 'Regression run of OPUS device-upgrade and activation orders on T4 at store-hour peak (1,800 store reps) with PERF TLG booked exclusively.',
    workItems: ['feature:705'],
    stories: 'Reused scenarios O1–O4 (lookup, upgrade, activation with CAS credit, BOBPM fallout); no new stories.',
    plan: '1,800 store reps, 20–40s think time, 30-minute steady state. Gates: upgrade submit p95 ≤ 4,000ms, CSI fault rate < 0.5%, BOBPM backlog drains within 15 minutes.',
    planWarnings: [],
    envSummary: 'T4 ready: CSI, CAS and BOBPM target T4 instances; PERF TLG booked exclusively and no batch job scheduled in the window.',
    envWarnings: [],
    script: 'Reused validated LoadRunner scripts opus_upgrade.c and opus_activation.c with T4 runtime parameters.',
    scriptRefs: ['memory://scripts/opus_upgrade.c', 'memory://scripts/opus_activation.c'],
    vusers: 1_800,
    rampS: 1_200,
    durationS: 1_800,
    slas: { p95_ms: 4_000, error_rate: 0.005 },
    executionSummary: 'Held 1,800 store reps for 30 minutes. Upgrade submit p95 3,420ms; CSI faults 0.3%; PERF TLG lock waits peaked at 0.9s; BOBPM backlog drained in 11 minutes.',
    executionWarnings: [],
    kpis: { tps_avg: 46.8, p95_ms: 3_420, error_rate: 0.003, vusers_peak: 1_800 },
    passed: true,
    breaches: [],
    executionNotes: 'All Legacy Perf store-peak gates passed.',
    reporting: 'Certified: all gates passed on T4. TLG commit latency is the largest tier contributor (1,950ms of the 3,420ms p95) with 15% headroom to the gate.',
    postmortem: 'No blocking actions. Track TLG commit latency trend across the next two releases.',
    dialogue: [],
    contextPacket: { source: 'jira_align', title: 'feature:705 store upgrade peak', summary: 'OPUS upgrade orders must sustain store-hour peak on T4 with PERF TLG booked exclusively.', ref: 'feature:705' },
  },
  'digital-baseline': {
    key: 'digital-baseline',
    project: 'digital-perf',
    participants: scope([['mule', 'perfhyper1'], ['idp', 'perfhyper1'], ['bsse', 'perf-bsse']]),
    primary: 'mule-perfhyper1',
    traffic: 'mule-perfhyper1',
    request: 'Load test of the Mule order-status and account-summary experience APIs at the digital-channel steady-state forecast (140 requests/second).',
    workItems: ['feature:640'],
    stories: 'Two API scenarios: D1 order-status and D2 account summary, both Mule > IDP > BSSe. No UI in scope.',
    plan: 'Open model at 140 req/s (D1 60%, D2 40%) for 60 minutes after a 15-minute warm-up. Gates: p95 ≤ 600ms, p99 ≤ 1,500ms, errors < 0.5%.',
    planWarnings: [],
    envSummary: 'PERFHYPER1 ready; Mule targets idp.perfhyper1; PERF BSSe booked for the window.',
    envWarnings: [],
    script: 'Reused mule-digital-soak.jmx with constant-throughput timers and OAuth token refresh.',
    scriptRefs: ['memory://scripts/mule-digital-soak.jmx'],
    vusers: 120,
    rampS: 900,
    durationS: 3_600,
    slas: { p95_ms: 600, error_rate: 0.005 },
    executionSummary: 'Sustained 140 req/s for 60 minutes: p95 470ms, p99 1,180ms, errors 0.2% (all 429s from the account-summary rate-limit policy).',
    executionWarnings: [],
    kpis: { tps_avg: 139.6, p95_ms: 470, error_rate: 0.002, vusers_peak: 120 },
    passed: true,
    breaches: [],
    executionNotes: 'All Digital Perf API gates passed.',
    reporting: 'Passed: order-status and account-summary APIs met p95, p99 and error gates. 429s were confined to the account-summary policy and stayed below 0.5%.',
    postmortem: 'No blocking actions. Review the account-summary rate-limit policy before the holiday window.',
    dialogue: [],
    contextPacket: { source: 'jira_align', title: 'feature:640 digital order status', summary: 'Order-status API must sustain digital-channel load with p95 ≤ 600ms.', ref: 'feature:640' },
  },
  'hrp-baseline': {
    key: 'hrp-baseline',
    project: 'hrp',
    participants: scope([['opus', 't3'], ['csi', 't3'], ['crm', 't3'], ['tlg', 'perf-tlg']]),
    primary: 'opus-t3',
    traffic: 'opus-t3',
    request: 'HRP headroom step test: OPUS activations and upgrades on T3 at 100% and 110% of forecast holiday peak against PERF TLG.',
    workItems: ['objective:88'],
    stories: 'H1 OPUS upgrade and H2 activation with CRM interaction notes, both committing to PERF TLG.',
    plan: 'Steps at 1,800 and 1,980 store reps, 15 minutes each. Hard stops: PERF TLG lock waits > 2s for 3 minutes or CSI faults > 5%.',
    planWarnings: [],
    envSummary: 'T3 ready; PERF TLG booked exclusively; T5 batch driver paused for the window.',
    envWarnings: [],
    script: 'Reused OPUS LoadRunner scripts with T3 runtime parameters.',
    scriptRefs: ['memory://scripts/opus_upgrade.c', 'memory://scripts/opus_activation.c'],
    vusers: 1_980,
    rampS: 900,
    durationS: 1_800,
    slas: { p95_ms: 4_000, error_rate: 0.01 },
    executionSummary: 'Both steps completed. At 110% of forecast, upgrade p95 was 3,880ms and PERF TLG lock waits peaked at 1.6s; CSI faults stayed at 0.6%.',
    executionWarnings: ['Headroom above 110% of forecast is not demonstrated; lock waits approach the 2s hard stop.'],
    kpis: { tps_avg: 51.2, p95_ms: 3_880, error_rate: 0.006, vusers_peak: 1_980 },
    passed: true,
    breaches: [],
    executionNotes: 'Gates passed at 110% of forecast; the 120% target remains untested.',
    reporting: 'Passed at 110% of forecast with limited margin: PERF TLG lock waits are the constraint. The 120% HRP target needs a retest with TLG tuning.',
    postmortem: 'Action: TLG team to review DB2 lock escalation on service-order commit before the 120% retest.',
    dialogue: [],
    contextPacket: { source: 'jira_align', title: 'objective:88 holiday peak readiness', summary: 'Demonstrate 20% headroom on PERF TLG above forecast holiday peak.', ref: 'objective:88' },
  },
}
