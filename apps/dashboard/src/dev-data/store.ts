import { ACC_DEMO_CONTEXT, ACC_DEMO_DEPENDENCIES, ACC_DEMO_TEST_PLAN, ACC_DEMO_TEST_DATA } from './telecomFixtures'
import type { components } from '@apex/api-client'
import type { Assistant, Run, Thread } from '@langchain/langgraph-sdk'

import { PHASE_NAMES, type PhaseName, type PipelineState } from '@apex/pipeline-events'

import { APPLICATION_PROMPT_TEMPLATE, SCRIPT_VARIANT_PROMPTS, TELECOM_PHASE_PROMPTS } from './telecomPrompts'
import {
  deterministicAgentReply,
  FLAGSHIP_ALIGN_QUERY,
  TELECOM_DATASET_VERSION,
  TELECOM_PROJECT_ID,
} from './telecomFixtures'

type Application = components['schemas']['ApplicationOut']
type Connection = components['schemas']['ConnectionOut']
type ConnectionCreate = components['schemas']['ConnectionCreate']
type ConnectionUpdate = components['schemas']['ConnectionUpdate']
type Consumer = components['schemas']['ConsumerRead']
type ConsumerCreate = components['schemas']['ConsumerCreateRequest']
type ConsumerCreated = components['schemas']['ConsumerCreated']
type ConsumerUpdate = components['schemas']['ConsumerUpdateRequest']
type ContextSummaryRequest = components['schemas']['ContextSummaryRequest']
type DocumentOut = components['schemas']['DocumentOut']
type DraftRead = components['schemas']['DraftRead']
type Environment = components['schemas']['EnvironmentOut']
type EvidencePacket = components['schemas']['EvidencePacket']
type HostMappingIn = components['schemas']['HostMappingIn']
type HostMappingOut = components['schemas']['HostMappingOut']
type InventoryView = components['schemas']['InventoryView']
type LogEntry = components['schemas']['LogEntryOut']
type LogSearchRequest = components['schemas']['LogSearchRequest']
type PipelineDetail = components['schemas']['PipelineDetail']
type PipelineSummary = components['schemas']['PipelineSummary']
type PortKind = components['schemas']['PortKind']
type ProbeResult = components['schemas']['ProbeResult']
type PromptDetail = components['schemas']['PromptDetail']
type PromptSummary = components['schemas']['PromptSummary']
type PromptVersionDetail = components['schemas']['PromptVersionDetail']
type SavedQuery = components['schemas']['SavedQueryOut']
type SavedQueryCreate = components['schemas']['SavedQueryCreate']
type SavedQueryUpdate = components['schemas']['SavedQueryUpdate']
type SystemInfo = components['schemas']['SystemInfo']
type StartPipelineRequest = components['schemas']['StartPipelineRequest']
type StartPipelineResponse = components['schemas']['StartPipelineResponse']
type RerunPipelineRequest = components['schemas']['RerunPipelineRequest']
type RerunPipelineResponse = components['schemas']['RerunPipelineResponse']
type TranslatedQuery = components['schemas']['ExecutableTranslatedQuery']
type ResolvedTranslatedQuery = components['schemas']['ResolvedTranslatedQuery']
type AgentAnalytics = components['schemas']['AgentAnalyticsResponse']
type AgentAnalyticsBreakdownRow = components['schemas']['AgentAnalyticsBreakdownRow']
type AgentAnalyticsSeriesPoint = components['schemas']['AgentAnalyticsSeriesPoint']
type AgentAnalyticsTotals = components['schemas']['AgentAnalyticsTotals']
type AgentGroupBy = AgentAnalytics['window']['group_by']
type UsageAnalytics = components['schemas']['UsageAnalyticsResponse']
type WorkItem = components['schemas']['WorkItem']
type WorkItemDraft = components['schemas']['WorkItemDraft']
type ResolvedWorkItem = components['schemas']['ResolvedWorkItem']
type WorkItemPage = components['schemas']['ResolvedWorkItemPage']
type WorkTrackingBinding = components['schemas']['WorkTrackingBindingOut']

type AgentSort =
  | 'key'
  | 'events'
  | 'errors'
  | 'input_tokens'
  | 'output_tokens'
  | 'total_tokens'
  | 'cache_read_tokens'
  | 'cache_creation_tokens'
  | 'reasoning_tokens'
  | 'cost_usd'
  | 'avg_latency_ms'
  | 'p95_latency_ms'
  | 'runs'

interface AgentMetricEvent {
  at: string
  thread_id: string
  thread_title: string
  project_id: string | null
  phase: PhaseName
  agent_name: string
  model: string
  provider: string
  status: 'ok' | 'error'
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_creation_tokens: number
  reasoning_tokens: number
  cost_usd: number
  latency_ms: number
}

export interface DevArtifactBytes {
  blob: Blob
  mediaType: string
  size: number
}

interface StoredArtifact {
  body: string | Uint8Array
  mediaType: string
}

interface DemoStreamEvent {
  id: string
  event: 'custom'
  data: Record<string, unknown>
}

interface ContextSummaryJob {
  runId: string
  threadId: string
  subject: string
  summary: string
}

interface PromptRecord extends PromptDetail {
  versions: PromptVersionDetail[]
}

interface ArrayDelta<T> {
  upsert: T[]
  removed: string[]
}

interface MapDelta<T> {
  upsert: Array<[string, T]>
  removed: string[]
}

interface DemoBaseline {
  applications: Application[]
  environments: Environment[]
  inventories: Map<string, InventoryView>
  connections: Connection[]
  hostMappings: Map<string, HostMappingOut[]>
  consumers: Consumer[]
  documents: DocumentOut[]
  drafts: DraftRead[]
  workItems: WorkItem[]
  workItemProviders: Map<string, 'jira' | 'jira_align'>
  savedQueries: SavedQuery[]
  prompts: PromptRecord[]
  pipelineDetails: Map<string, PipelineDetail>
  runs: Map<string, Run[]>
  assistants: Assistant[]
  artifacts: Map<string, StoredArtifact>
  runStages: Map<string, string>
  apexScenarios: Array<Record<string, unknown>>
  apexSchedules: Array<Record<string, unknown>>
  apexOccurrences: Array<Record<string, unknown>>
}

const JSON_HEADERS = { 'content-type': 'application/json' }
export const DEMO_STATE_STORAGE_KEY = 'apex.demo.state.v1'
const DEMO_STATE_VERSION = 3
const EXECUTION_SAMPLE_DELAY_MS = import.meta.env.MODE === 'test' ? 5 : 3_000
const AGENT_GROUP_BYS: AgentGroupBy[] = ['model', 'stage', 'agent', 'date', 'test']
const AGENT_SORTS: AgentSort[] = [
  'key',
  'events',
  'errors',
  'input_tokens',
  'output_tokens',
  'total_tokens',
  'cache_read_tokens',
  'cache_creation_tokens',
  'reasoning_tokens',
  'cost_usd',
  'avg_latency_ms',
  'p95_latency_ms',
  'runs',
]

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS })
}

function emptyResponse(status = 204): Response {
  return new Response(null, { status })
}

function problemResponse(status: number, title: string, detail: string): Response {
  return jsonResponse({ type: 'about:blank', title, status, detail }, status)
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function arrayDelta<T>(current: T[], baseline: T[], keyOf: (item: T) => string): ArrayDelta<T> | undefined {
  const before = new Map(baseline.map((item) => [keyOf(item), item]))
  const after = new Map(current.map((item) => [keyOf(item), item]))
  const upsert = current.filter((item) => JSON.stringify(before.get(keyOf(item))) !== JSON.stringify(item))
  const removed = [...before.keys()].filter((key) => !after.has(key))
  return upsert.length || removed.length ? { upsert: clone(upsert), removed } : undefined
}

function applyArrayDelta<T>(baseline: T[], delta: ArrayDelta<T> | undefined, keyOf: (item: T) => string): T[] {
  const cleanBaseline = clone(baseline)
  if (!delta) return cleanBaseline
  const removed = new Set(delta.removed)
  const cleanUpsert = clone(delta.upsert)
  const updates = new Map(cleanUpsert.map((item) => [keyOf(item), item]))
  const result = cleanBaseline.filter((item) => !removed.has(keyOf(item))).map((item) => updates.get(keyOf(item)) ?? item)
  const baselineKeys = new Set(cleanBaseline.map(keyOf))
  return [...cleanUpsert.filter((item) => !baselineKeys.has(keyOf(item))), ...result]
}

function mapDelta<T>(current: Map<string, T>, baseline: Map<string, T>): MapDelta<T> | undefined {
  const upsert = [...current].filter(([key, value]) => JSON.stringify(baseline.get(key)) !== JSON.stringify(value))
  const removed = [...baseline.keys()].filter((key) => !current.has(key))
  return upsert.length || removed.length ? { upsert: clone(upsert), removed } : undefined
}

function applyMapDelta<T>(baseline: Map<string, T>, delta: MapDelta<T> | undefined): Map<string, T> {
  const result = new Map<string, T>(clone([...baseline]))
  if (!delta) return result
  for (const key of delta.removed) result.delete(key)
  for (const [key, value] of clone(delta.upsert)) result.set(key, value)
  return result
}

function decodePart(value: string | undefined): string {
  return decodeURIComponent(value ?? '')
}

function paginate<T>(items: T[], params: URLSearchParams): { items: T[]; limit: number; offset: number; total: number } {
  const limit = Number(params.get('limit') ?? '25')
  const offset = Number(params.get('offset') ?? '0')
  return { items: items.slice(offset, offset + limit), limit, offset, total: items.length }
}

function matchesText(values: Array<string | null | undefined>, q: string | null): boolean {
  if (!q) return true
  const needle = q.toLowerCase()
  return values.some((value) => value?.toLowerCase().includes(needle))
}

function nowIso(): string {
  return new Date().toISOString()
}

function isoMinutesAgo(minutes: number, anchor = nowIso()): string {
  return new Date(new Date(anchor).getTime() - minutes * 60_000).toISOString()
}

function parseMulti(params: URLSearchParams, name: string): string[] {
  return params
    .getAll(name)
    .flatMap((value) => value.split(','))
    .map((value) => value.trim())
    .filter(Boolean)
}

function parsePositiveInt(value: string | null, fallback: number): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed >= 0 ? Math.trunc(parsed) : fallback
}

function percentile95(values: number[]): number | null {
  if (values.length === 0) return null
  const sorted = [...values].sort((a, b) => a - b)
  const index = Math.max(0, Math.ceil(sorted.length * 0.95) - 1)
  return sorted[index] ?? null
}

function agentGroupKey(event: AgentMetricEvent, groupBy: AgentGroupBy): string {
  if (groupBy === 'stage') return event.phase
  if (groupBy === 'agent') return event.agent_name
  if (groupBy === 'date') return event.at.slice(0, 10)
  if (groupBy === 'test') return event.thread_id
  return event.model
}

function agentBucketStart(at: string, bucket: 'day' | 'hour'): string {
  const date = new Date(at)
  if (bucket === 'hour') date.setUTCMinutes(0, 0, 0)
  else date.setUTCHours(0, 0, 0, 0)
  return date.toISOString()
}

function agentTotals(events: AgentMetricEvent[]): AgentAnalyticsTotals {
  const latencies = events.map((event) => event.latency_ms)
  return {
    events: events.length,
    errors: events.filter((event) => event.status === 'error').length,
    input_tokens: events.reduce((sum, event) => sum + event.input_tokens, 0),
    output_tokens: events.reduce((sum, event) => sum + event.output_tokens, 0),
    total_tokens: events.reduce((sum, event) => sum + event.input_tokens + event.output_tokens, 0),
    cache_read_tokens: events.reduce((sum, event) => sum + event.cache_read_tokens, 0),
    cache_creation_tokens: events.reduce((sum, event) => sum + event.cache_creation_tokens, 0),
    reasoning_tokens: events.reduce((sum, event) => sum + event.reasoning_tokens, 0),
    cost_usd: Number(events.reduce((sum, event) => sum + event.cost_usd, 0).toFixed(6)),
    avg_latency_ms: events.length
      ? Math.round(events.reduce((sum, event) => sum + event.latency_ms, 0) / events.length)
      : null,
    p95_latency_ms: percentile95(latencies),
    latency_samples: latencies.length,
    runs: new Set(events.map((event) => event.thread_id)).size,
    agents: new Set(events.map((event) => event.agent_name)).size,
    models: new Set(events.map((event) => event.model)).size,
  }
}

function agentBreakdownRow(key: string, events: AgentMetricEvent[]): AgentAnalyticsBreakdownRow {
  const totals = agentTotals(events)
  return {
    key,
    events: totals.events,
    errors: totals.errors,
    input_tokens: totals.input_tokens,
    output_tokens: totals.output_tokens,
    total_tokens: totals.total_tokens,
    cache_read_tokens: totals.cache_read_tokens,
    cache_creation_tokens: totals.cache_creation_tokens,
    reasoning_tokens: totals.reasoning_tokens,
    cost_usd: totals.cost_usd,
    avg_latency_ms: totals.avg_latency_ms,
    p95_latency_ms: totals.p95_latency_ms,
    latency_samples: totals.latency_samples,
    runs: totals.runs,
    thread_id: events.every((event) => event.thread_id === key) ? key : null,
  }
}

function agentSeriesPoint(bucketStart: string, key: string, events: AgentMetricEvent[]): AgentAnalyticsSeriesPoint {
  const totals = agentTotals(events)
  return {
    bucket_start: bucketStart,
    key,
    events: totals.events,
    errors: totals.errors,
    input_tokens: totals.input_tokens,
    output_tokens: totals.output_tokens,
    total_tokens: totals.total_tokens,
    cache_read_tokens: totals.cache_read_tokens,
    cache_creation_tokens: totals.cache_creation_tokens,
    reasoning_tokens: totals.reasoning_tokens,
    cost_usd: totals.cost_usd,
    avg_latency_ms: totals.avg_latency_ms,
    p95_latency_ms: totals.p95_latency_ms,
    latency_samples: totals.latency_samples,
    runs: totals.runs,
  }
}

function makeStrip(
  overrides: Partial<Record<PhaseName, Partial<components['schemas']['PhaseStripEntry']>>> = {},
): components['schemas']['PhaseStripEntry'][] {
  return PHASE_NAMES.map((phase) => ({
    phase,
    status: 'pending',
    attempt: null,
    ...overrides[phase],
  }))
}

function artifactRef(
  id: string,
  name: string,
  kind: string,
  mediaType: string,
  summary?: string,
  anchor?: string,
) {
  return {
    id,
    name,
    kind,
    media_type: mediaType,
    uri: `memory://reports/${id}`,
    summary: summary ?? null,
    created_at: isoMinutesAgo(35, anchor),
  }
}

function makePipelineState(
  title: string,
  currentPhase: PhaseName,
  status: 'busy' | 'interrupted' | 'idle' | 'error',
  anchor?: string,
): PipelineState {
  const report = artifactRef('exec-report', 'load-report.json', 'report', 'application/json', 'Normalized KPI summary', anchor)
  const transcript = artifactRef(
    'execution-transcript',
    'execution transcript.txt',
    'transcript',
    'text/plain',
    'Execution agent transcript',
    anchor,
  )
  const archive = artifactRef('results-archive', 'results.zip', 'archive', 'application/octet-stream', 'Raw engine results', anchor)
  const promptReviews = Object.fromEntries(
    PHASE_NAMES.map((phase) => [
      phase,
      {
        system: `You are the ${phase.replaceAll('_', ' ')} phase operator.`,
        phase_prompt: `Prepare the ${phase.replaceAll('_', ' ')} deliverable for: ${title}.`,
        application: 'ACC add-a-line requirements: preserve order idempotency, monitor BSSe p95, MQ depth, APIM limits, and AKS scaling.',
        additional_context: ACC_DEMO_CONTEXT,
        source: { origin: 'catalog', ref: `phase/${phase}@dev` },
        updated_at: isoMinutesAgo(140, anchor),
        updated_by: 'system',
      },
    ]),
  ) as PipelineState['prompt_reviews']
  return {
    title,
    request: ACC_DEMO_CONTEXT,
    current_phase: currentPhase,
    phases_plan: [...PHASE_NAMES],
    prompt_reviews: promptReviews,
    phase_results: {
      story_analysis: {
        phase: 'story_analysis',
        status: 'succeeded',
        attempt: 1,
        started_at: isoMinutesAgo(140, anchor),
        ended_at: isoMinutesAgo(138, anchor),
        duration_s: 105,
        summary: ACC_DEMO_CONTEXT,
        reasoning_digest: 'Prioritized cross-system paths with asynchronous back-pressure and duplicate-order risk.',
        resolved_prompt: {
          system: 'You are the story analysis phase operator.',
          user: 'Analyze feature:583 and story:9012 for performance-test scope.',
        },
        resolved_prompt_source: { origin: 'catalog', ref: 'phase/story_analysis/system@v3' },
      },
      test_planning: {
        phase: 'test_planning',
        status: status === 'interrupted' && currentPhase === 'test_planning' ? 'awaiting_prompt_review' : 'succeeded',
        attempt: 1,
        started_at: isoMinutesAgo(132, anchor),
        ended_at: isoMinutesAgo(128, anchor),
        duration_s: 220,
        summary: 'Planned load, stress, and endurance coverage with a stepped ramp to 300 users and 85 TPS.',
        warnings: ['BSSe create-order is within 20% of its configured p95 budget.'],
      },
      env_triage: {
        phase: 'env_triage',
        status: 'succeeded',
        attempt: 1,
        started_at: isoMinutesAgo(125, anchor),
        ended_at: isoMinutesAgo(123, anchor),
        duration_s: 94,
        summary: 'PERFHYPER2 is reachable; mulesoft-add-line-api points at bsse-other-test and MQ has two consumers instead of four.',
        warnings: ['non-PERFHYPER2 BSSe test endpoint would invalidate results until corrected.', 'MQ consumer capacity is below the approved reference.'],
      },
      script_scenario: {
        phase: 'script_scenario',
        status: 'succeeded',
        attempt: 1,
        started_at: isoMinutesAgo(118, anchor),
        ended_at: isoMinutesAgo(112, anchor),
        duration_s: 360,
        summary: 'Generated correlated JMeter and VuGen scripts with synthetic ACC add-a-line data and explicit SLA assertions.',
        load_test_spec: {
          title,
          target_environment: 'env-perf',
          vusers: 300,
          ramp_s: 300,
          duration_s: 1500,
          slas: { p95_ms: 3000, error_rate: 0.02 },
          script_refs: ['memory://scripts/acc-add-line.jmx', 'memory://scripts/acc_add_line.c'],
        },
      },
      execution: {
        phase: 'execution',
        status: status === 'busy' ? 'running' : status === 'error' ? 'failed' : 'succeeded',
        attempt: status === 'error' ? 2 : 1,
        started_at: isoMinutesAgo(90, anchor),
        ended_at: status === 'busy' ? null : isoMinutesAgo(58, anchor),
        duration_s: status === 'busy' ? null : 1920,
        summary:
          status === 'error'
            ? 'Execution orchestration failed while collecting the simulated engine result.'
            : 'Execution reached 300 users; BSSe p95 breached 3,000ms while MQ backlog recovered after a ramp hold.',
        warnings: status === 'error' ? ['Result collection requires an operator retry.'] : ['BSSe create-order p95 exceeded SLA and warrants a Jira Cloud defect.'],
        artifact_ids: [report.id, archive.id, transcript.id],
        engine: 'apexload',
        engine_started_at: isoMinutesAgo(89, anchor),
        engine_handle: { engine: 'apexload', connection_id: 'conn-apex-load', external_run_id: 'AL-DEMO-2603' },
        test_summary: {
          engine: 'apexload',
          passed: false,
          kpis: status === 'error' ? {} : { tps_avg: 82.7, p95_ms: 3650, error_rate: 0.028, vusers_peak: 300 },
          sla_breaches: status === 'error' ? [] : ['oms_create_order_p95_ms', 'error_rate'],
          notes: status === 'error' ? 'Collection did not complete.' : 'Orchestration succeeded; the tested release is a conditional fail pending BSSe/MQ remediation.',
        },
        transcript_ref: transcript,
      },
      reporting: {
        phase: 'reporting',
        status:
          status === 'interrupted' && currentPhase === 'reporting'
            ? 'awaiting_output_review'
            : status === 'busy'
              ? 'pending'
              : 'succeeded',
        attempt: status === 'busy' ? undefined : 1,
        started_at: status === 'busy' ? null : isoMinutesAgo(50, anchor),
        ended_at: status === 'busy' ? null : isoMinutesAgo(45, anchor),
        duration_s: status === 'busy' ? null : 290,
        summary: 'Drafted executive and technical reports with Jira Cloud defect PERF-248.',
      },
      postmortem: {
        phase: 'postmortem',
        status: 'pending',
      },
    },
    artifacts: [report, archive, transcript],
    dialogue: [
      {
        id: 'dlg-1',
        phase: 'test_planning',
        role: 'operator',
        content: 'Include asynchronous MQ backlog and retry-idempotency coverage.',
        at: isoMinutesAgo(127, anchor),
      },
      {
        id: 'dlg-2',
        phase: 'test_planning',
        role: 'agent',
        content: 'The workload now covers both paths with stepped-ramp checkpoints.',
        at: isoMinutesAgo(126, anchor),
      },
    ],
    context_packets: [
      {
        id: 'ctx-1',
        source: 'jira_align',
        title: 'story:9012 acceptance criteria',
        summary: 'ACC add-a-lines must remain idempotent while fulfillment traverses IBM MQ.',
        ref: 'story:9012',
      },
    ],
    engine_handle: { engine: 'apexload', connection_id: 'conn-apex-load', external_run_id: 'AL-DEMO-2603' },
  }
}

export class DevDataStore {
  private nextId = 100
  private applications: Application[]
  private environments: Environment[]
  private inventories = new Map<string, InventoryView>()
  private connections: Connection[]
  private hostMappings = new Map<string, HostMappingOut[]>()
  private consumers: Consumer[]
  private documents: DocumentOut[]
  private drafts: DraftRead[]
  private evidence: EvidencePacket[]
  private workItems: WorkItem[]
  private workItemProviders = new Map<string, 'jira' | 'jira_align'>()
  private savedQueries: SavedQuery[]
  private prompts: PromptRecord[]
  private logs: LogEntry[]
  private pipelineDetails = new Map<string, PipelineDetail>()
  private runs = new Map<string, Run[]>()
  private assistants: Assistant[]
  private artifacts = new Map<string, StoredArtifact>()
  private runStages = new Map<string, string>()
  private runEvents = new Map<string, DemoStreamEvent[]>()
  private completedRunStreams = new Set<string>()
  private contextSummaryJobs = new Map<string, ContextSummaryJob>()
  private apexIdempotency = new Map<string, { status: number; body: Record<string, unknown> }>()
  private reportingTickets = new Map<string, string>()
  private pipelineLaunches = new Map<string, StartPipelineResponse>()
  private baseline!: DemoBaseline
  private activationTime = nowIso()
  private apexScenarios: Array<Record<string, unknown>> = [
    { id: 'scenario-esim-peak', name: 'ACC add-a-line peak certification', status: 'ready', protocol: 'mixed-jmeter-vugen' },
    { id: 'scenario-mq-recovery', name: 'IBM MQ backlog recovery', status: 'ready', protocol: 'jmeter' },
  ]
  private apexSchedules: Array<Record<string, unknown>> = [
    { id: 'schedule-release', scenario_id: 'scenario-esim-peak', name: 'Release candidate certification', status: 'enabled', approval_id: 'approval-release' },
    { id: 'schedule-nightly', scenario_id: 'scenario-esim-peak', name: 'Nightly ACC add-a-line baseline', status: 'enabled', approval_id: 'approval-nightly' },
  ]
  private apexOccurrences: Array<Record<string, unknown>> = [
    { id: 'occurrence-1', schedule_id: 'schedule-release', status: 'approved', scheduled_offset_minutes: -240, approval_id: 'approval-release' },
    { id: 'occurrence-2', schedule_id: 'schedule-nightly', status: 'pending_approval', scheduled_offset_minutes: -1_680, approval_id: 'approval-next' },
  ]

  readonly systemInfo: SystemInfo = {
    name: 'APEX Orchestration Engine',
    version: TELECOM_DATASET_VERSION,
    environment: 'offline-demo',
    features: { engines: true, documents: true, dummy_data: true },
    limits: { max_context_packets: 32 },
    consumer: {
      name: 'Demo Presenter',
      role: 'admin',
      scopes: [{ project_id: TELECOM_PROJECT_ID, app_id: null }],
    },
  }

  constructor() {
    this.activationTime = this.persistedActivationTime()
    const created = isoMinutesAgo(8_000, this.activationTime)
    const updated = isoMinutesAgo(20, this.activationTime)
    this.applications = []
    this.environments = []
    this.connections = []
    this.consumers = []
    this.documents = []
    this.drafts = []
    this.evidence = []
    this.workItems = []
    this.savedQueries = []
    this.seedTelecomCatalog(created, updated)
    this.prompts = this.makePrompts(created, updated)
    this.logs = this.makeLogs()
    this.assistants = this.makeAssistants(created, updated)
    this.seedPipelines()
    this.seedArtifacts()
    this.baseline = this.captureBaseline()
    this.restorePersistedState()
  }

  private persistedActivationTime(): string {
    if (typeof localStorage === 'undefined') return nowIso()
    try {
      const parsed = JSON.parse(localStorage.getItem(DEMO_STATE_STORAGE_KEY) ?? 'null') as Record<string, unknown> | null
      return parsed?.['version'] === DEMO_STATE_VERSION && typeof parsed['activationTime'] === 'string'
        ? parsed['activationTime']
        : nowIso()
    } catch {
      return nowIso()
    }
  }

  private captureBaseline(): DemoBaseline {
    return {
      applications: clone(this.applications),
      environments: clone(this.environments),
      inventories: new Map(clone([...this.inventories])),
      connections: clone(this.connections),
      hostMappings: new Map(clone([...this.hostMappings])),
      consumers: clone(this.consumers),
      documents: clone(this.documents),
      drafts: clone(this.drafts),
      workItems: clone(this.workItems),
      workItemProviders: new Map(clone([...this.workItemProviders])),
      savedQueries: clone(this.savedQueries),
      prompts: clone(this.prompts),
      pipelineDetails: new Map(clone([...this.pipelineDetails])),
      runs: new Map(clone([...this.runs])),
      assistants: clone(this.assistants),
      artifacts: new Map(clone([...this.artifacts])),
      runStages: new Map(clone([...this.runStages])),
      apexScenarios: clone(this.apexScenarios),
      apexSchedules: clone(this.apexSchedules),
      apexOccurrences: clone(this.apexOccurrences),
    }
  }

  private restoreBaseline(): void {
    this.applications = clone(this.baseline.applications)
    this.environments = clone(this.baseline.environments)
    this.inventories = new Map(clone([...this.baseline.inventories]))
    this.connections = clone(this.baseline.connections)
    this.hostMappings = new Map(clone([...this.baseline.hostMappings]))
    this.consumers = clone(this.baseline.consumers)
    this.documents = clone(this.baseline.documents)
    this.drafts = clone(this.baseline.drafts)
    this.workItems = clone(this.baseline.workItems)
    this.workItemProviders = new Map(clone([...this.baseline.workItemProviders]))
    this.savedQueries = clone(this.baseline.savedQueries)
    this.prompts = clone(this.baseline.prompts)
    this.pipelineDetails = new Map(clone([...this.baseline.pipelineDetails]))
    this.runs = new Map(clone([...this.baseline.runs]))
    this.assistants = clone(this.baseline.assistants)
    this.artifacts = new Map(clone([...this.baseline.artifacts]))
    this.runStages = new Map(clone([...this.baseline.runStages]))
    this.runEvents.clear()
    this.completedRunStreams.clear()
    this.contextSummaryJobs.clear()
    this.apexIdempotency.clear()
    this.reportingTickets.clear()
    this.pipelineLaunches.clear()
    this.apexScenarios = clone(this.baseline.apexScenarios)
    this.apexSchedules = clone(this.baseline.apexSchedules)
    this.apexOccurrences = clone(this.baseline.apexOccurrences)
  }

  private seedTelecomCatalog(created: string, updated: string): void {
    const project = TELECOM_PROJECT_ID
    this.applications = [
      { id: 'app-digital-device', name: 'ACC', project_id: project, description: 'ACC is the employee-facing web application for selling mobile phones, cellular plans, and internet service. Demo scope: add a line to an existing customer cellular plan.', archived_at: null, created_at: created, updated_at: updated },
      { id: 'app-order-fulfillment', name: 'Mulesoft', project_id: project, description: 'Test integration dependency connecting ACC in PERFHYPER2 to BSSe; infrastructure and telemetry are synthetic demo assumptions.', archived_at: null, created_at: created, updated_at: updated },
      { id: 'app-usage-billing', name: 'BSSe', project_id: project, description: 'Test billing dependency for existing accounts, plan eligibility, line orders, and resulting charges.', archived_at: null, created_at: created, updated_at: updated },
    ]
    this.environments = [
      this.environment('env-perf', 'app-digital-device', 'PERFHYPER2', 'k8s', 'https://acc.perfhyper2.example.test', false),
      this.environment('env-perf-dr', 'app-digital-device', 'PERFHYPER2-DR', 'k8s', 'https://acc.perfhyper2-dr.example.test', true),
      this.environment('env-qc-reference', 'app-usage-billing', 'BSSe TEST', 'vm', 'https://bsse.perfhyper2.example.test', false),
      this.environment('env-production', 'app-order-fulfillment', 'Mulesoft TEST', 'k8s', 'https://mulesoft.perfhyper2.example.test', true),
    ]
    this.inventories.clear()
    const inventoryServices = [
      { name: 'acc-web', image: 'registry.example.test/acc-web:4.8.2', replicas: 4 },
      { name: 'mulesoft-add-line-api', image: 'registry.example.test/order-mgmt:7.14.1', replicas: 3 },
      { name: 'mq-fulfillment-consumer', image: 'registry.example.test/mq-consumer:3.6.0', replicas: 2 },
      { name: 'bsse-account-api', image: 'registry.example.test/usage-profile:2.9.4', replicas: 3 },
    ]
    for (const env of this.environments) {
      this.inventories.set(env.id, { environment_id: env.id, snapshot: { scanned_at: updated, stale: env.id !== 'env-perf', services: inventoryServices.map((service) => ({ ...service, replicas: env.id === 'env-perf' ? service.replicas : Math.max(1, service.replicas - 1) })) } })
    }
    this.connections = [
      this.connection('conn-align', 'work_tracking', 'jira_align', 'Jira Align Portfolio', project, 'https://align.example.test', true, { base_url: 'https://align.example.test', program_id: '42' }, null),
      this.connection('conn-jira', 'work_tracking', 'jira', 'Jira Cloud Performance Defects', project, 'https://jira.example.test', true, { base_url: 'https://jira.example.test', user_email: 'demo-presenter@example.test', project_key: 'PERF' }, null),
      this.connection('conn-apex-load', 'execution_engine', 'apex_load', 'APEX Load PERFHYPER2 Pool', project, null, true, { pool: 'perf-demo' }, null),
      this.connection('conn-observe', 'observability', 'stub', 'Dynatrace + Prometheus', project, null, true, {}, null),
      this.connection('conn-logs', 'log_search', 'elasticsearch', 'Telecom ELK', project, 'https://elk.example.test', true, {}, null),
      this.connection('conn-docs', 'documents', 'stub', 'Performance Knowledge Base', project, null, true, {}, null),
      this.connection('conn-inventory', 'cluster_inventory', 'k8s', 'AKS PERFHYPER2 Cluster', project, null, true, { kube_context: 'aks-perf-demo' }, null),
      this.connection('conn-source', 'source_control', 'stub', 'Script Repository', project, null, true, {}, null),
      this.connection('conn-artifacts', 'artifact_store', 'stub', 'Demo Artifact Store', project, null, true, {}, null),
    ]
    this.hostMappings.clear()
    this.hostMappings.set('conn-jira', [{ id: 'map-jira-1', pattern: 'jira.internal.example.test', target: 'jira.example.test', enabled: true }])
    this.consumers = [
      this.consumer('cons-admin', 'Demo Presenter', 'admin', 'dashboard', true, 'd3m00001'),
      this.consumer('cons-operator', 'Performance Operator', 'operator', 'dashboard', true, 'd3m00002'),
      this.consumer('cons-viewer', 'Release Reviewer', 'viewer', 'dashboard', true, 'd3m00003'),
      this.consumer('cons-ci', 'Nightly Certification', 'operator', 'headless', true, 'd3m00004'),
    ]
    this.documents = [
      this.document('doc-architecture', 'acc-add-line-architecture.pdf', 'application/pdf', 1_420_000, ACC_DEMO_CONTEXT),
      this.document('doc-sla', 'acc-add-line-demo-sla.md', 'text/markdown', 18_420, 'Synthetic demo SLAs and release thresholds; confirm before a real test.'),
      this.document('doc-traffic', 'peak-hour-traffic-profile.csv', 'text/csv', 9_810, 'Synthetic employee-session volumes and API transaction targets.'),
      this.document('doc-runbook', 'mq-backlog-response-runbook.md', 'text/markdown', 21_200, 'Ramp-hold and queue recovery procedure.'),
      this.document('doc-baselines', 'historical-baselines.json', 'application/json', 14_660, 'Five synthetic baseline runs.'),
      this.document('doc-data', 'synthetic-test-data-contract.md', 'text/markdown', 8_140, 'Synthetic existing accounts with active plans, current line counts, line limits, compatible devices, SIMs, expected charges, and rejection cases. Exclusive account allocation and cleanup after each run.'),
    ]
    this.drafts = [
      this.draft('draft-esim', 'ACC Add-a-Line Peak Certification', project, {
        launch_idempotency_key: 'demo-esim-launch',
        title: 'ACC Add-a-Line Peak Certification',
        request: ACC_DEMO_CONTEXT,
        scope: { project_id: project, app_id: 'app-digital-device', environment_id: 'env-perf' },
        work_items: [
          { key: 'feature:583', connection_id: 'conn-align', provider: 'jira_align' },
          { key: 'story:9012', connection_id: 'conn-align', provider: 'jira_align' },
        ],
        document_ids: ['doc-architecture', 'doc-sla', 'doc-traffic', 'doc-data'],
        context_summary_ids: [],
        config: { engine: 'apexload', phases: null, prompt_focus_phase: 'story_analysis', gates_mode: 'all_gated', golden_config_id: 'asst-release', golden_configurable: null },
        prompt_overrides: {}, prompt_override_removals: [],
      }),
    ]
    this.evidence = Array.from({ length: 12 }, (_, index) => ({
      id: `evidence-${index + 1}`,
      source: index < 4 ? 'jira_align' : index < 8 ? 'observability' : 'documents',
      title: ['ACC add-a-line acceptance criteria', 'BSSe fulfillment path', 'Retry idempotency', 'Peak traffic objective', 'BSSe latency baseline', 'MQ depth baseline', 'AKS HPA history', 'BSSe read baseline', 'Architecture packet', 'SLA standard', 'Backlog runbook', 'Synthetic data contract'][index]!,
      summary: ['ACC must add exactly one line and one set of charges to the existing cellular plan, preserving all existing lines.', 'ACC in PERFHYPER2 submits through Mulesoft TEST to BSSe TEST. APIM, AKS, and MQ telemetry is illustrative.', 'Every retry carries the same idempotency key.', 'Illustrative target: 85 aggregate API TPS at 300 employee sessions; measure completed add-line journeys separately.', 'Historical BSSe p95 is 2.4 seconds.', 'Normal fulfillment depth is below 2,000.', 'HPA reaches six pods within two minutes.', 'Usage profile p95 is 180ms.', 'ACC PERFHYPER2 connects to Mulesoft TEST and BSSe TEST; infrastructure details are illustrative.', 'BSSe create-order p95 must remain below 3,000ms.', 'Hold the ramp while backlog stabilizes.', 'All accounts and devices are synthetic.'][index]!,
      ref: index < 4 ? ['feature:583', 'story:9012', 'story:9013', 'objective:77'][index]! : `demo:evidence:${index + 1}`,
      thread_id: index % 3 === 0 ? 'run-conditional' : null,
    }))
    const align: WorkItem[] = [
      { key: 'epic:310', title: 'Employee sales modernization', kind: 'epic', status: 'In Progress', description: 'Modernize sales and service journeys.', url: 'https://align.example.test/epics/310' },
      { key: 'capability:420', title: 'Existing customer plan management', kind: 'capability', status: 'In Progress', description: 'Add and activate a line on an existing cellular plan through ACC.', url: 'https://align.example.test/capabilities/420', parent_key: 'epic:310' },
      { key: 'feature:583', title: 'ACC add-a-line orchestration', kind: 'feature', status: 'Ready for Test', description: 'Add one line to an existing cellular plan in ACC on PERFHYPER2 through Mulesoft and BSSe TEST, preserving existing lines and preventing duplicate charges.', url: 'https://align.example.test/features/583', parent_key: 'capability:420', priority: 'Critical', sprint: 'PI 26.3' },
      { key: 'story:9012', title: 'Employee completes ACC add-a-line with resilient order fulfillment', kind: 'story', status: 'In Progress', description: ACC_DEMO_CONTEXT, url: 'https://align.example.test/stories/9012', parent_key: 'feature:583', assignee: 'Morgan Lee', sprint: 'PI 26.3 / I2' },
      { key: 'story:9013', title: 'ACC add-a-line idempotent submission retry', kind: 'story', status: 'Ready for Test', description: 'Reuse the original idempotency key on network retry.', url: 'https://align.example.test/stories/9013', parent_key: 'feature:583' },
      { key: 'story:9014', title: 'Existing account and plan eligibility lookup', kind: 'story', status: 'Accepted', description: 'Read eligibility state from BSSe before adding a line.', url: 'https://align.example.test/stories/9014', parent_key: 'feature:583' },
      { key: 'bug:9441', title: 'Fulfillment events queue during peak line additions', kind: 'bug', status: 'Open', description: 'IBM MQ depth rises when BSSe consumers lag.', url: 'https://align.example.test/bugs/9441', parent_key: 'story:9012', severity: 'High' },
      { key: 'objective:77', title: 'Peak-hour ACC reliability', kind: 'objective', status: 'In Progress', description: 'Meet peak release certification targets.', url: 'https://align.example.test/objectives/77' },
      { key: 'feature:590', title: 'ACC line activation status', kind: 'feature', status: 'Pending Approval', description: 'Expose asynchronous activation status.', url: 'https://align.example.test/features/590', parent_key: 'capability:420' },
      { key: 'story:9050', title: 'Employee views line activation status', kind: 'story', status: 'Open', description: 'Poll status with bounded backoff.', url: 'https://align.example.test/stories/9050', parent_key: 'feature:590' },
      { key: 'bug:9460', title: 'Activation polling ignores Retry-After', kind: 'bug', status: 'Open', description: 'Polling can overload APIM during delays.', url: 'https://align.example.test/bugs/9460', parent_key: 'story:9050' },
      { key: 'story:9061', title: 'ACC add-a-line line-limit rejection', kind: 'story', status: 'Funnel', description: 'Reject accounts at the line limit without creating an order or charge.', url: 'https://align.example.test/stories/9061', parent_key: 'feature:583' },
    ]
    const jira: WorkItem[] = [
      { key: 'PERF-240', title: '[Perf] BSSe create-order baseline drift', kind: 'bug', status: 'Done', description: 'Resolved baseline regression from the prior PI.', url: 'https://jira.example.test/browse/PERF-240', priority: 'Medium', labels: ['perf-test'] },
      { key: 'PERF-241', title: '[Perf] MQ consumer scaling validation', kind: 'task', status: 'In Progress', description: 'Validate consumer scaling from two to four replicas.', url: 'https://jira.example.test/browse/PERF-241', priority: 'High', labels: ['perf-test', 'mq'], linked_keys: ['feature:583'] },
      { key: 'PERF-242', title: '[Perf] APIM rate-limit headroom', kind: 'task', status: 'Open', description: 'Confirm peak headroom for add-a-line APIs.', url: 'https://jira.example.test/browse/PERF-242', priority: 'Medium', labels: ['perf-test', 'apim'] },
      { key: 'PERF-243', title: '[Perf] BSSe eligibility read p95', kind: 'bug', status: 'Done', description: 'Track eligibility lookup latency.', url: 'https://jira.example.test/browse/PERF-243', priority: 'Low', labels: ['perf-test', 'bsse'] },
      { key: 'PERF-244', title: '[Perf] HPA stabilization window', kind: 'task', status: 'Open', description: 'Tune HPA stabilization for ramp tests.', url: 'https://jira.example.test/browse/PERF-244', priority: 'Medium', labels: ['perf-test', 'aks'] },
      { key: 'PERF-245', title: '[Perf] Synthetic device pool refresh', kind: 'task', status: 'In Progress', description: 'Refresh synthetic ACC add-a-line inventory.', url: 'https://jira.example.test/browse/PERF-245', priority: 'Medium', labels: ['perf-test', 'test-data'] },
      { key: 'PERF-246', title: '[Perf] Mulesoft connection pool telemetry', kind: 'task', status: 'Open', description: 'Add pool saturation telemetry.', url: 'https://jira.example.test/browse/PERF-246', priority: 'Low', labels: ['perf-test', 'mulesoft'] },
      { key: 'PERF-247', title: '[Perf] Nightly ACC add-a-line certification', kind: 'task', status: 'Open', description: 'Schedule the release-candidate certification.', url: 'https://jira.example.test/browse/PERF-247', priority: 'High', labels: ['perf-test', 'release'] },
    ]
    this.workItems = [...align, ...jira]
    this.workItemProviders.clear()
    for (const item of align) this.workItemProviders.set(item.key, 'jira_align')
    for (const item of jira) this.workItemProviders.set(item.key, 'jira')
    this.savedQueries = [
      { id: 'sq-align-esim', name: 'ACC add-a-line release scope', provider: 'jira_align', query: FLAGSHIP_ALIGN_QUERY, description: 'Flagship release scope', project_id: project, connection_id: 'conn-align', created_by: 'Demo Presenter', created_at: created, updated_at: updated },
      { id: 'sq-align-risk', name: 'Open Align delivery risks', provider: 'jira_align', query: "programId eq 42 and itemtype in ('bug') and state eq 'Open'", description: 'Portfolio risks', project_id: project, connection_id: 'conn-align', created_by: 'Demo Presenter', created_at: created, updated_at: updated },
      { id: 'sq-jira-open', name: 'Open performance defects', provider: 'jira', query: 'project = PERF AND status in (Open, "In Progress") AND labels = perf-test', description: 'Active engineering actions', project_id: project, connection_id: 'conn-jira', created_by: 'Demo Presenter', created_at: created, updated_at: updated },
      { id: 'sq-jira-release', name: 'Release blockers', provider: 'jira', query: 'project = PERF AND priority in (Highest, High) AND status != Done', description: 'Release review queue', project_id: project, connection_id: 'conn-jira', created_by: 'Demo Presenter', created_at: created, updated_at: updated },
    ]
  }

  async handleApexRequest(request: Request): Promise<Response | null> {
    const url = new URL(request.url)
    const path = url.pathname.replace(/\/+$/, '') || '/'
    const method = request.method.toUpperCase()
    if (!path.startsWith('/v1/')) return null

    let response: Response
    if (method === 'GET' && path === '/v1/system/info') response = jsonResponse(this.systemInfo)
    else if (method === 'GET' && path === '/v1/analytics/agents') response = jsonResponse(this.agentAnalytics(url.searchParams))
    else if (method === 'GET' && path === '/v1/analytics/usage') response = jsonResponse(this.usage(url.searchParams))
    else if (method === 'POST' && path === '/v1/logs/search') response = jsonResponse(await this.searchLogs(request))
    else if (path.startsWith('/v1/pipelines')) response = await this.handlePipelines(method, path, request, url.searchParams)
    else if (path.startsWith('/v1/catalog/applications')) response = await this.handleApplications(method, path, request, url.searchParams)
    else if (path.startsWith('/v1/catalog/environments')) response = await this.handleEnvironments(method, path, request, url.searchParams)
    else if (path.startsWith('/v1/inventory/environments')) response = this.handleInventory(method, path)
    else if (path.startsWith('/v1/work-tracking')) response = await this.handleWorkTracking(method, path, request, url.searchParams)
    else if (path.startsWith('/v1/context')) response = await this.handleContext(method, path, request, url.searchParams)
    else if (path.startsWith('/v1/documents')) response = await this.handleDocuments(method, path, request, url.searchParams)
    else if (path.startsWith('/v1/drafts')) response = await this.handleDrafts(method, path, request, url.searchParams)
    else if (path.startsWith('/v1/prompts')) response = await this.handlePrompts(method, path, request, url.searchParams)
    else if (path.startsWith('/v1/admin/connections')) response = await this.handleConnections(method, path, request, url.searchParams)
    else if (path.startsWith('/v1/admin/consumers')) response = await this.handleConsumers(method, path, request)
    else if (path.startsWith('/v1/apex-load')) response = await this.handleApexLoad(method, path, request, url.searchParams)
    else response = problemResponse(501, 'demo_handler_missing', `Offline demo has no handler for ${method} ${path}.`)

    if (!['GET', 'HEAD'].includes(method) && response.ok) this.persistState()
    return response
  }

  getArtifactBytes(url: string): DevArtifactBytes | null {
    const parsed = new URL(url, window.location.origin)
    if (!parsed.pathname.startsWith('/v1/artifacts/')) return null
    const key = parsed.pathname
      .slice('/v1/artifacts/'.length)
      .split('/')
      .map(decodeURIComponent)
      .join('/')
    const artifact = this.artifacts.get(key)
    if (!artifact) return null
    const blob = new Blob([artifact.body as BlobPart], { type: artifact.mediaType })
    return { blob, mediaType: artifact.mediaType, size: blob.size }
  }

  getFixtureStats(): Record<string, number> {
    return {
      applications: this.applications.length,
      environments: this.environments.length,
      connections: this.connections.length,
      consumers: this.consumers.length,
      documents: this.documents.length,
      evidencePackets: this.evidence.length,
      jiraAlignItems: this.workItems.filter((item) => this.workItemProviders.get(item.key) === 'jira_align').length,
      initialJiraCloudItems: this.workItems.filter((item) => this.workItemProviders.get(item.key) === 'jira').length,
      savedQueries: this.savedQueries.length,
      prompts: this.prompts.length,
      seededRuns: this.pipelineDetails.size,
      logs: this.logs.length,
      phaseMetricEvents: this.agentMetricEvents('day').length,
      usageDays: 14,
      artifacts: this.artifacts.size,
    }
  }

  handleRawRequest(request: Request): Response | null {
    const path = new URL(request.url).pathname
    const match = /^\/threads\/([^/]+)\/runs\/([^/]+)\/stream$/.exec(path)
    if (!match) return null
    const threadId = decodePart(match[1])
    const runId = decodePart(match[2])
    const job = this.contextSummaryJobs.get(runId)
    if (!job || job.threadId !== threadId) {
      return problemResponse(404, 'demo_summary_not_found', `Offline summary run ${runId} was not found.`)
    }
    const body = [
      'event: values',
      `data: ${JSON.stringify({ values: { summary: job.summary } })}`,
      '',
      '',
    ].join('\n')
    return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-store' } })
  }

  searchAssistants(): Assistant[] {
    return clone(this.assistants)
  }

  getAssistant(assistantId: string): Assistant {
    const assistant = this.assistants.find((item) => item.assistant_id === assistantId)
    if (!assistant) throw new Error(`Demo assistant ${assistantId} not found`)
    return clone(assistant)
  }

  updateAssistant(assistantId: string, payload: { config?: { configurable?: Record<string, unknown> } }): Assistant {
    const index = this.assistants.findIndex((item) => item.assistant_id === assistantId)
    if (index === -1) throw new Error(`Demo assistant ${assistantId} not found`)
    const current = this.assistants[index]!
    const updated: Assistant = {
      ...current,
      config: { ...(current.config ?? {}), configurable: payload.config?.configurable ?? {} },
      version: current.version + 1,
      updated_at: nowIso(),
    }
    this.assistants[index] = updated
    this.persistState()
    return clone(updated)
  }

  createThread(payload?: { metadata?: Record<string, unknown> }): Thread<Record<string, unknown>> {
    const id = this.id('thread')
    const created = nowIso()
    const detail = this.pipelineDetail(id, 'Untitled telecom certification', payload?.metadata?.['project_id'] as string | undefined, 'busy', 'story_analysis')
    this.initializeFreshPipeline(detail, 'story_analysis')
    this.pipelineDetails.set(id, detail)
    this.runs.set(id, [])
    this.runStages.set(id, 'story_analysis')
    this.persistState()
    return {
      thread_id: id,
      created_at: created,
      updated_at: created,
      state_updated_at: created,
      metadata: payload?.metadata ?? {},
      status: 'busy',
      values: detail.values ?? {},
      interrupts: {},
    }
  }

  createRun(threadId: string | null, assistantId: string, payload?: { input?: unknown; config?: { configurable?: Record<string, unknown> } }): Run {
    const id = this.id('run')
    const tid = threadId ?? this.createThread().thread_id
    const now = nowIso()
    const input = (payload?.input ?? {}) as Record<string, unknown>
    const title = typeof input['title'] === 'string' ? input['title'] : this.pipelineDetails.get(tid)?.title || 'Telecom certification rerun'
    const firstPhase = Array.isArray(payload?.config?.configurable?.['phases'])
      ? (payload?.config?.configurable?.['phases'] as string[])[0]
      : this.pipelineDetails.get(tid)?.current_phase ?? 'story_analysis'
    const phase = PHASE_NAMES.includes(firstPhase as PhaseName) ? (firstPhase as PhaseName) : 'story_analysis'
    const detail = this.pipelineDetails.get(tid) ?? this.pipelineDetail(tid, title, 'telecom-modernization', 'busy', phase)
    detail.title = title
    detail.thread_status = 'busy'
    detail.current_phase = phase
    detail.updated_at = now
    detail.pending_gate = null
    detail.interrupts = []
    detail.values = {
      ...(detail.values ?? {}),
      title,
      request: typeof input['request'] === 'string' ? input['request'] : (detail.values?.['request'] as string | undefined),
      current_phase: phase,
    }
    this.pipelineDetails.set(tid, detail)
    const run: Run = {
      run_id: id,
      thread_id: tid,
      assistant_id: assistantId,
      created_at: now,
      updated_at: now,
      status: 'running',
      metadata: {},
      multitask_strategy: 'reject',
    }
    this.runs.set(tid, [run, ...(this.runs.get(tid) ?? [])])
    if (Array.isArray(payload?.config?.configurable?.['phases']) || !this.runStages.has(tid)) this.runStages.set(tid, phase)
    this.persistState()
    return clone(run)
  }

  listRuns(threadId: string): Run[] {
    return clone(this.runs.get(threadId) ?? [])
  }

  async *joinRunStream(
    threadId: string | undefined | null,
    runId: string,
    options?: { signal?: AbortSignal; lastEventId?: string; streamMode?: string },
  ): AsyncGenerator<{ id?: string; event: string; data: unknown }> {
    const tid = threadId ?? 'thread'
    const detail = this.pipelineDetails.get(tid)
    if (!detail) return
    const stage = this.runStages.get(tid) ?? 'story_analysis'
    const committed = this.runEvents.get(runId) ?? []
    let replayFrom = 0
    if (options?.lastEventId) {
      const cursorIndex = committed.findIndex((event) => event.id === options.lastEventId)
      if (cursorIndex === -1) throw new Error(`Unknown resume cursor ${options.lastEventId}`)
      replayFrom = cursorIndex + 1
    }
    for (const event of committed.slice(replayFrom)) yield clone(event)
    if (this.completedRunStreams.has(runId)) return

    const emit = (suffix: string, data: Record<string, unknown>): DemoStreamEvent | null => {
      const id = `${runId}-${suffix}`
      const history = this.runEvents.get(runId) ?? []
      if (history.some((event) => event.id === id)) return null
      const event: DemoStreamEvent = { id, event: 'custom', data: { schema_version: 1, ...data } }
      history.push(event)
      this.runEvents.set(runId, history)
      this.persistState()
      return event
    }
    const phase = async function* (name: PhaseName, attempt = 1) {
      const start = emit(`${name}-start`, { type: 'phase_status', phase: name, status: 'running', attempt })
      if (start) yield start
      const agent = emit(`${name}-agent`, { type: 'agent_message', phase: name, model: 'offline-deterministic-template', chars: 640 + name.length * 17 })
      if (agent) yield agent
      const done = emit(`${name}-done`, { type: 'phase_status', phase: name, status: 'succeeded', attempt })
      if (done) yield done
    }
    const samples = [
      { progress_pct: 8, vusers: 0, tps: 0, error_rate: 0, p95_ms: 0, status: 'provisioning' },
      { progress_pct: 24, vusers: 100, tps: 31, error_rate: 0.002, p95_ms: 680, status: 'running' },
      { progress_pct: 45, vusers: 200, tps: 62, error_rate: 0.006, p95_ms: 1_820, status: 'running' },
      { progress_pct: 62, vusers: 260, tps: 79, error_rate: 0.015, p95_ms: 3_180, status: 'running' },
      { progress_pct: 76, vusers: 260, tps: 77, error_rate: 0.018, p95_ms: 3_420, status: 'running' },
      { progress_pct: 91, vusers: 300, tps: 85, error_rate: 0.028, p95_ms: 3_650, status: 'running' },
      { progress_pct: 100, vusers: 0, tps: 84.7, error_rate: 0.028, p95_ms: 3_650, status: 'completed' },
    ] as const
    const rerunPhases = stage === 'rerun_subset'
      ? ((detail.values?.['demo_rerun_phases'] as PhaseName[] | undefined) ?? [])
      : []
    const plan = emit('plan', {
      type: 'plan_resolved',
      phases: rerunPhases.length ? rerunPhases : PHASE_NAMES,
    })
    if (plan) yield plan

    if (stage === 'rerun_subset') {
      for (const rerunPhase of rerunPhases) {
        const priorAttempt = detail.phase_strip.find((item) => item.phase === rerunPhase)?.attempt ?? 0
        this.setPhaseState(detail, rerunPhase, 'running', priorAttempt + 1)
        detail.current_phase = rerunPhase
        for await (const event of phase(rerunPhase, priorAttempt + 1)) yield event
        this.setPhaseState(
          detail,
          rerunPhase,
          'succeeded',
          priorAttempt + 1,
          `Offline rerun regenerated the ${rerunPhase.replaceAll('_', ' ')} result from the checkpointed configuration.`,
        )
      }
      const lastPhase = rerunPhases.at(-1)
      const gatesMode = detail.values?.['demo_rerun_gates_mode']
      if (lastPhase && gatesMode !== 'auto') {
        const attempt = detail.phase_strip.find((item) => item.phase === lastPhase)?.attempt ?? 1
        this.setPhaseState(detail, lastPhase, 'awaiting_output_review', attempt)
        detail.current_phase = lastPhase
        detail.thread_status = 'interrupted'
        detail.values = { ...(detail.values ?? {}), demo_rerun_gate: true }
        this.addPhaseGate(detail, lastPhase)
        const gate = emit('rerun-gate', {
          type: 'gate_opened',
          gate: 'phase_review',
          phase: lastPhase,
          attempt,
        })
        if (gate) yield gate
      } else {
        detail.thread_status = 'idle'
        detail.pending_gate = null
        detail.interrupts = []
        detail.values = { ...(detail.values ?? {}), demo_rerun_gate: false }
      }
    } else if (stage === 'rerun_reopen') {
      const rerunPhase = detail.current_phase as PhaseName
      const attempt = detail.phase_strip.find((item) => item.phase === rerunPhase)?.attempt ?? 1
      this.setPhaseState(detail, rerunPhase, 'awaiting_output_review', attempt + 1)
      detail.thread_status = 'interrupted'
      detail.values = { ...(detail.values ?? {}), demo_rerun_gate: true }
      this.addPhaseGate(detail, rerunPhase, true)
      const gate = emit('rerun-regate', {
        type: 'gate_opened',
        gate: 'phase_review',
        phase: rerunPhase,
        attempt: attempt + 1,
      })
      if (gate) yield gate
    } else if (stage === 'rerun_complete') {
      const rerunPhase = detail.current_phase as PhaseName
      const current = detail.phase_strip.find((item) => item.phase === rerunPhase)
      if (current?.status !== 'skipped') {
        this.setPhaseState(detail, rerunPhase, 'succeeded', current?.attempt ?? 1)
      }
      detail.thread_status = 'idle'
      detail.pending_gate = null
      detail.interrupts = []
      detail.values = { ...(detail.values ?? {}), demo_rerun_gate: false }
      const done = emit('rerun-complete', {
        type: 'phase_status',
        phase: rerunPhase,
        status: current?.status === 'skipped' ? 'skipped' : 'succeeded',
        attempt: current?.attempt ?? 1,
      })
      if (done) yield done
    } else if (stage === 'story_analysis') {
      for await (const event of phase('story_analysis')) yield event
      this.setPhaseState(detail, 'story_analysis', 'succeeded', 1, 'Resolved the Jira Align hierarchy and peak-load scenarios.')
      this.setPhaseState(detail, 'test_planning', 'awaiting_prompt_review')
      detail.current_phase = 'test_planning'
      detail.thread_status = 'interrupted'
      this.addPromptGate(detail)
      const event = emit('prompt-gate', { type: 'gate_opened', gate: 'prompt_review', phase: 'test_planning', attempt: 1 })
      if (event) yield event
    } else if (stage === 'prompt_reopen') {
      this.setPhaseState(detail, 'test_planning', 'awaiting_prompt_review', 2)
      this.addPromptGate(detail, true)
      detail.thread_status = 'interrupted'
      const event = emit('prompt-regate', { type: 'gate_opened', gate: 'prompt_review', phase: 'test_planning', attempt: 2 })
      if (event) yield event
    } else if (stage === 'env_triage' || stage === 'env_discuss' || stage === 'env_revised') {
      if (stage === 'env_triage') for await (const event of phase('test_planning')) yield event
      this.setPhaseState(detail, 'test_planning', 'succeeded', stage === 'env_triage' ? 1 : 2)
      this.setPhaseState(
        detail,
        'env_triage',
        'awaiting_output_review',
        stage === 'env_revised' ? 2 : 1,
        stage === 'env_revised'
          ? 'PERFHYPER2 rescan is green after correcting the BSSe endpoint and MQ consumer count.'
          : 'non-PERFHYPER2 BSSe test target and insufficient MQ consumers require operator review.',
      )
      detail.current_phase = 'env_triage'
      detail.thread_status = 'interrupted'
      this.addPhaseGate(detail, 'env_triage', stage === 'env_revised')
      const event = emit('env-gate', { type: 'gate_opened', gate: 'phase_review', phase: 'env_triage', attempt: stage === 'env_triage' ? 1 : 2 })
      if (event) yield event
    } else if (stage === 'execution') {
      this.setPhaseState(detail, 'env_triage', 'succeeded', 2, 'PERFHYPER2 configuration correction and rescan passed.')
      for await (const event of phase('script_scenario')) yield event
      this.setPhaseState(detail, 'script_scenario', 'succeeded', 1, 'Validated JMeter and VuGen scenarios are ready.')
      this.setPhaseState(detail, 'execution', 'running')
      const provision = emit('provision-tool', { type: 'tool_call', phase: 'execution', id: `${runId}-apex-load`, tool: 'apex_load.start', status: 'ok' })
      if (provision) yield provision
      const started = emit('execution-start', { type: 'phase_status', phase: 'execution', status: 'running', attempt: 1 })
      if (started) yield started
      const committedProgress = Number(detail.values?.['execution_progress'] ?? -1)
      for (const [index, sample] of samples.entries()) {
        if (options?.signal?.aborted) return
        if (sample.progress_pct <= committedProgress) continue
        const event = emit(`engine-${index}`, { type: 'engine_poll', phase: 'execution', attempt: 1, engine: 'apexload', external_run_id: `AL-${tid}`, status: sample.status, progress_pct: sample.progress_pct, live_stats: { vusers: sample.vusers, tps: sample.tps, error_rate: sample.error_rate, p95_ms: sample.p95_ms }, mq_depth: index >= 3 ? 8_400 + index * 1_670 : 1_400, hpa_replicas: index < 3 ? 4 : index < 5 ? 6 : 8, note: index === 3 ? 'Ramp held while IBM MQ depth stabilizes.' : undefined })
        if (event) yield event
        detail.values = { ...(detail.values ?? {}), execution_progress: sample.progress_pct, live_metrics: sample }
        this.persistState()
        await new Promise((resolve) => setTimeout(resolve, EXECUTION_SAMPLE_DELAY_MS))
      }
      const done = emit('execution-done', { type: 'phase_status', phase: 'execution', status: 'succeeded', attempt: 1 })
      if (done) yield done
      this.setPhaseState(detail, 'execution', 'awaiting_output_review', 1, 'BSSe p95 exceeded SLA while orchestration completed successfully.')
      const execution = (detail.values as PipelineState).phase_results?.execution
      if (execution) {
        execution.test_summary = {
          engine: 'apexload',
          passed: false,
          kpis: { tps_avg: 82.7, p95_ms: 3650, error_rate: 0.028, vusers_peak: 300 },
          sla_breaches: ['oms_create_order_p95_ms', 'error_rate'],
          notes: 'The simulated test failed its SLA checks.',
        }
      }
      detail.current_phase = 'execution'
      detail.thread_status = 'interrupted'
      this.addPhaseGate(detail, 'execution')
      const gate = emit('execution-gate', { type: 'gate_opened', gate: 'phase_review', phase: 'execution', attempt: 1 })
      if (gate) yield gate
    } else if (stage === 'execution_reopen') {
      this.setPhaseState(detail, 'execution', 'awaiting_output_review', 2)
      detail.current_phase = 'execution'
      detail.thread_status = 'interrupted'
      this.addPhaseGate(detail, 'execution', true)
      const event = emit('execution-regate', { type: 'gate_opened', gate: 'phase_review', phase: 'execution', attempt: 2 })
      if (event) yield event
    } else if (stage === 'engine_recovery') {
      const tool = emit('engine-reconcile', { type: 'tool_call', phase: 'execution', id: `${runId}-engine-reconcile`, tool: 'apex_load.reconcile', status: 'ok' })
      if (tool) yield tool
      const poll = emit('engine-recovered', { type: 'engine_poll', phase: 'execution', attempt: 2, engine: 'apexload', external_run_id: `AL-${tid}`, status: 'provisioning', progress_pct: 8, live_stats: { vusers: 0, tps: 0, error_rate: 0, p95_ms: 0 }, note: 'Checkpoint reconciliation reused the original idempotency key.' })
      if (poll) yield poll
      this.setPhaseState(detail, 'execution', 'awaiting_output_review', 2, 'Provisioning reconciliation succeeded without creating a duplicate native run.')
      detail.thread_status = 'interrupted'
      this.addPhaseGate(detail, 'execution', true)
      const gate = emit('engine-recovery-gate', { type: 'gate_opened', gate: 'phase_review', phase: 'execution', attempt: 2 })
      if (gate) yield gate
    } else if (stage === 'reporting') {
      this.setPhaseState(detail, 'execution', 'succeeded', 1)
      for await (const event of phase('reporting')) yield event
      this.setPhaseState(detail, 'reporting', 'awaiting_output_review', 1, 'Executive and technical reports plus the Jira defect draft are ready.')
      detail.current_phase = 'reporting'
      detail.thread_status = 'interrupted'
      this.addPhaseGate(detail, 'reporting')
      const event = emit('report-gate', { type: 'gate_opened', gate: 'phase_review', phase: 'reporting', attempt: 1 })
      if (event) yield event
    } else if (stage === 'complete') {
      const ticket = this.ensurePerf248(tid)
      const jira = emit('jira-create', { type: 'tool_call', phase: 'reporting', id: `${runId}-jira-create`, tool: 'jira.create', status: 'ok', ticket_key: ticket.key })
      if (jira) yield jira
      this.setPhaseState(detail, 'reporting', 'succeeded', 1)
      for await (const event of phase('postmortem')) yield event
      this.setPhaseState(detail, 'postmortem', 'succeeded', 1, 'MQ consumer, HPA, and BSSe remediation actions were recorded.')
      detail.current_phase = 'postmortem'
      detail.thread_status = 'idle'
      detail.pending_gate = null
      detail.interrupts = []
      detail.values = { ...(detail.values ?? {}), generated_ticket: ticket, postmortem_actions: ['Scale IBM MQ fulfillment consumers from two to four.', 'Tune the AKS HPA stabilization window.', 'Optimize the BSSe create-order dependency path.'] }
    } else if (stage === 'prompt_playground') {
      const promptPhase = PHASE_NAMES.includes(detail.current_phase as PhaseName)
        ? (detail.current_phase as PhaseName)
        : 'story_analysis'
      for await (const event of phase(promptPhase)) yield event
      this.setPhaseState(
        detail,
        promptPhase,
        'succeeded',
        1,
        `Offline prompt simulation completed for ${String(detail.values?.['prompt_key'] ?? promptPhase)}.`,
      )
      detail.thread_status = 'idle'
      detail.pending_gate = null
      detail.interrupts = []
      detail.values = {
        ...(detail.values ?? {}),
        prompt_playground_result: {
          model: 'offline-deterministic-template',
          summary: `Deterministic ${promptPhase.replaceAll('_', ' ')} response generated without an LLM.`,
          sample_input: detail.values?.['sample_input'] ?? {},
        },
      }
    } else if (stage === 'aborted') {
      this.setPhaseState(detail, detail.current_phase as PhaseName, 'aborted')
      detail.thread_status = 'error'
      detail.pending_gate = null
      detail.interrupts = []
      detail.values = { ...(detail.values ?? {}), run_aborted: true }
      const event = emit('aborted', { type: 'phase_status', phase: detail.current_phase, status: 'aborted', attempt: 1 })
      if (event) yield event
    }
    detail.updated_at = nowIso()
    this.runs.set(tid, (this.runs.get(tid) ?? []).map((run) => run.run_id === runId ? { ...run, updated_at: nowIso(), status: detail.thread_status === 'interrupted' ? 'interrupted' : detail.thread_status === 'idle' ? 'success' : detail.thread_status === 'error' ? 'error' : run.status } : run))
    this.completedRunStreams.add(runId)
    this.persistState()
  }

  private handlePipelines(method: string, path: string, request: Request, params: URLSearchParams): Promise<Response> | Response {
    if (method === 'POST' && path === '/v1/pipelines') {
      return request.json().then((raw) => {
        const body = raw as StartPipelineRequest
        const idempotencyKey = body.idempotency_key?.trim() || null
        const replay = idempotencyKey ? this.pipelineLaunches.get(idempotencyKey) : null
        if (replay) return jsonResponse(replay, 202)
        const thread = this.createThread({ metadata: { project_id: body.project_id ?? TELECOM_PROJECT_ID } })
        const configurable = { ...(body.configurable ?? {}) }
        const configuredPhases = body.phases ?? configurable['phases']
        if (Array.isArray(configuredPhases)) configurable['phases'] = configuredPhases
        const run = this.createRun(thread.thread_id, body.assistant_id ?? 'pipeline', {
          input: { title: body.title, request: body.request },
          config: { configurable },
        })
        const detail = this.pipelineDetails.get(thread.thread_id)!
        detail.project_id = body.project_id ?? TELECOM_PROJECT_ID
        detail.app_id = body.app_id ?? 'app-digital-device'
        const state = detail.values as PipelineState
        state.context_packets = (body.context_packets ?? state.context_packets ?? []).map((packet, index) => ({
          ...packet,
          id: packet.id ?? `launch-context-${index + 1}`,
        }))
        state.run_config = configurable
        detail.values = { ...state, document_ids: body.document_ids ?? [] } as Record<string, unknown>
        const response: StartPipelineResponse = {
          thread_id: thread.thread_id,
          run_id: run.run_id,
          stream_url: `/threads/${thread.thread_id}/runs/${run.run_id}/stream`,
        }
        if (idempotencyKey) this.pipelineLaunches.set(idempotencyKey, response)
        this.persistState()
        return jsonResponse(response, 202)
      })
    }
    const rerunMatch = path.match(/^\/v1\/pipelines\/([^/]+)\/rerun$/)
    if (method === 'POST' && rerunMatch) {
      return request.json().then((raw) => {
        const threadId = decodePart(rerunMatch[1]!)
        const detail = this.pipelineDetails.get(threadId)
        if (!detail) return problemResponse(404, 'not_found', `Run ${threadId} was not found.`)
        const body = raw as Partial<RerunPipelineRequest>
        const phases = [...new Set(body.phases ?? [])].filter((phase): phase is PhaseName =>
          PHASE_NAMES.includes(phase as PhaseName),
        )
        if (!body.idempotency_key?.trim() || !body.phases?.length || phases.length !== body.phases.length) {
          return problemResponse(422, 'validation_error', 'Reruns require an idempotency key and one or more valid phases.')
        }
        if (!['inherit', 'gated', 'auto'].includes(body.gates_mode ?? '')) {
          return problemResponse(422, 'validation_error', 'gates_mode must be inherit, gated, or auto.')
        }
        const launchKey = `rerun:${threadId}:${body.idempotency_key.trim()}`
        const replay = this.pipelineLaunches.get(launchKey)
        if (replay) return jsonResponse({ run_id: replay.run_id } satisfies RerunPipelineResponse, 202)
        if (detail.thread_status === 'busy' || detail.thread_status === 'interrupted') {
          return problemResponse(409, 'rerun_already_active', 'Finish or abort the active run before starting a rerun.')
        }
        const run = this.createRun(threadId, 'pipeline', {
          input: { title: detail.title },
          config: { configurable: { phases } },
        })
        detail.values = {
          ...(detail.values ?? {}),
          demo_rerun_phases: phases,
          demo_rerun_gates_mode: body.gates_mode,
          demo_rerun_gate: false,
        }
        this.runStages.set(threadId, 'rerun_subset')
        this.pipelineLaunches.set(launchKey, {
          thread_id: threadId,
          run_id: run.run_id,
          stream_url: `/threads/${threadId}/runs/${run.run_id}/stream`,
        })
        this.persistState()
        return jsonResponse({ run_id: run.run_id } satisfies RerunPipelineResponse, 202)
      })
    }
    if (method === 'GET' && path === '/v1/pipelines') {
      let items = [...this.pipelineDetails.values()].map((detail) => this.toSummary(detail))
      const status = params.get('status')
      const project = params.get('project')
      const q = params.get('q')
      if (status) items = items.filter((item) => item.thread_status === status)
      if (project) items = items.filter((item) => item.project_id === project)
      if (q) items = items.filter((item) => matchesText([item.title, item.thread_id, item.project_id, item.app_id], q))
      items.sort((a, b) => (b.updated_at ?? '').localeCompare(a.updated_at ?? ''))
      const page = paginate(items, params)
      return jsonResponse({ items: clone(page.items), limit: page.limit, offset: page.offset, total: page.total })
    }
    const parts = path.split('/')
    const threadId = decodePart(parts[3])
    if (parts[4] === 'phases' && parts[6] === 'prompt-review') {
      const detail = this.pipelineDetails.get(threadId)
      if (!detail) return problemResponse(404, 'not_found', `Run ${threadId} was not found.`)
      const phase = decodePart(parts[5]) as PhaseName
      const values = detail.values as PipelineState
      const reviews = { ...(values.prompt_reviews ?? {}) }
      const existing = reviews[phase]
      const entry = values.phase_results?.[phase]
      const fallback = existing ?? {
        system: entry?.resolved_prompt?.system ?? `You are the ${phase.replaceAll('_', ' ')} phase operator.`,
        phase_prompt: entry?.resolved_prompt?.user ?? `Prepare the ${phase.replaceAll('_', ' ')} deliverable.`,
        application: detail.app_id ? `Application requirements for ${detail.app_id}.` : null,
        additional_context: ACC_DEMO_CONTEXT,
        source: entry?.resolved_prompt_source ?? { origin: 'catalog', ref: `phase/${phase}@dev` },
        updated_at: detail.updated_at ?? nowIso(),
        updated_by: 'system',
      }
      if (method === 'GET') return jsonResponse(fallback)
      if (method === 'PATCH') {
        return request.json().then((body) => {
          const patch = body as Partial<typeof fallback>
          const review = {
            system: patch.system ?? fallback.system,
            phase_prompt: patch.phase_prompt ?? fallback.phase_prompt,
            application: patch.application === undefined ? fallback.application : patch.application,
            additional_context: patch.additional_context ?? fallback.additional_context,
            source: { origin: 'run_override' as const, ref: fallback.source?.ref ?? null, editor: 'dev' },
            updated_at: nowIso(),
            updated_by: 'dev',
          }
          reviews[phase] = review
          values.prompt_reviews = reviews
          detail.values = values as Record<string, unknown>
          detail.updated_at = nowIso()
          return jsonResponse(review)
        })
      }
    }
    if (method === 'GET' && parts.length === 4) {
      const detail = this.pipelineDetails.get(threadId)
      return detail ? jsonResponse(this.syncSummary(detail)) : problemResponse(404, 'not_found', `Run ${threadId} was not found.`)
    }
    if (method === 'POST' && parts[4] === 'abort') {
      const detail = this.pipelineDetails.get(threadId)
      if (detail) {
        detail.thread_status = 'error'
        detail.updated_at = nowIso()
        detail.values = { ...(detail.values ?? {}), run_aborted: true }
      }
      this.runs.set(
        threadId,
        (this.runs.get(threadId) ?? []).map((run) => ({ ...run, status: run.status === 'running' ? 'interrupted' : run.status })),
      )
      return jsonResponse({ cancelled_run_ids: (this.runs.get(threadId) ?? []).map((run) => run.run_id) }, 202)
    }
    if (method === 'POST' && parts[4] === 'gates' && parts[6] === 'resume') {
      return request.json().then((body) => {
        const detail = this.pipelineDetails.get(threadId)
        if (!detail) return problemResponse(404, 'not_found', `Run ${threadId} was not found.`)
        const interruptId = decodePart(parts[5])
        if (!detail.pending_gate || detail.pending_gate.interrupt_id !== interruptId) return problemResponse(409, 'gate_superseded', 'This gate was already actioned or replaced by a newer interrupt.')
        const requestBody = body as { action?: string; message?: string | null; note?: string | null; instructions?: string | null; prompt?: Record<string, unknown> | null }
        const action = requestBody.action ?? 'approve'
        const gatePhase = detail.pending_gate.phase as PhaseName
        const gateKind = detail.pending_gate.kind
        const isRerunGate = detail.values?.['demo_rerun_gate'] === true
        if (requestBody.message || requestBody.note || requestBody.instructions) this.appendDialogue(detail, gatePhase, requestBody.message ?? requestBody.note ?? requestBody.instructions ?? '')
        if (requestBody.prompt) {
          detail.values = { ...(detail.values ?? {}), prompt_edit_history: [...((detail.values?.['prompt_edit_history'] as unknown[]) ?? []), { at: nowIso(), interrupt_id: interruptId, prompt: requestBody.prompt }] }
        }
        detail.pending_gate = null
        detail.interrupts = []
        detail.thread_status = action === 'abort' ? 'error' : 'busy'
        detail.updated_at = nowIso()
        if (action === 'skip_phase') this.setPhaseState(detail, gatePhase, 'skipped')
        if (action === 'abort') this.runStages.set(threadId, 'aborted')
        else if (isRerunGate && ['discuss', 'revise'].includes(action)) this.runStages.set(threadId, 'rerun_reopen')
        else if (isRerunGate) this.runStages.set(threadId, 'rerun_complete')
        else if (action === 'modify') this.runStages.set(threadId, 'prompt_reopen')
        else if (gateKind === 'engine_provision_retry' && action === 'retry') this.runStages.set(threadId, 'engine_recovery')
        else if (gatePhase === 'test_planning') this.runStages.set(threadId, 'env_triage')
        else if (gatePhase === 'env_triage' && action === 'discuss') this.runStages.set(threadId, 'env_discuss')
        else if (gatePhase === 'env_triage' && action === 'revise') this.runStages.set(threadId, 'env_revised')
        else if (gatePhase === 'env_triage') this.runStages.set(threadId, 'execution')
        else if (gatePhase === 'execution' && ['discuss', 'revise'].includes(action)) this.runStages.set(threadId, 'execution_reopen')
        else if (gatePhase === 'execution') this.runStages.set(threadId, 'reporting')
        else if (gatePhase === 'reporting' && ['discuss', 'revise'].includes(action)) this.runStages.set(threadId, 'reporting')
        else if (gatePhase === 'reporting') this.runStages.set(threadId, 'complete')
        if (action === 'abort') {
          const now = nowIso()
          const run: Run = {
            run_id: this.id('run'),
            thread_id: threadId,
            assistant_id: 'pipeline',
            created_at: now,
            updated_at: now,
            status: 'running',
            metadata: {},
            multitask_strategy: 'reject',
          }
          this.runs.set(threadId, [run, ...(this.runs.get(threadId) ?? [])])
          this.persistState()
          return jsonResponse({ run_id: run.run_id }, 202)
        }
        const nextStage = this.runStages.get(threadId)
        if (nextStage === 'env_triage') {
          this.setPhaseState(detail, 'test_planning', 'succeeded', 2)
          this.setPhaseState(detail, 'env_triage', 'running')
        } else if (nextStage === 'env_discuss' || nextStage === 'env_revised') {
          this.setPhaseState(detail, 'env_triage', 'running', nextStage === 'env_revised' ? 2 : 1)
        } else if (nextStage === 'execution') {
          this.setPhaseState(detail, 'env_triage', 'succeeded', 2)
          this.setPhaseState(detail, 'script_scenario', 'succeeded', 1)
          this.setPhaseState(detail, 'execution', 'running')
        } else if (nextStage === 'execution_reopen' || nextStage === 'engine_recovery') {
          this.setPhaseState(detail, 'execution', 'running', 2)
        } else if (nextStage === 'reporting') {
          this.setPhaseState(detail, 'execution', 'succeeded', 1)
          this.setPhaseState(detail, 'reporting', 'running')
        } else if (nextStage === 'complete') {
          this.ensurePerf248(threadId)
          this.setPhaseState(detail, 'reporting', 'succeeded', 1)
          this.setPhaseState(detail, 'postmortem', 'running')
        }
        const run = this.createRun(threadId, 'pipeline', { input: {} })
        return jsonResponse({ run_id: run.run_id }, 202)
      })
    }
    return problemResponse(501, 'dummy_handler_missing', `Dummy data has no handler for ${method} ${path}.`)
  }

  private async handleApplications(method: string, path: string, request: Request, params: URLSearchParams): Promise<Response> {
    if (method === 'GET' && path === '/v1/catalog/applications') {
      const project = params.get('project')
      const includeArchived = params.get('include_archived') === 'true'
      const apps = this.applications.filter(
        (app) => (!project || app.project_id === project) && (includeArchived || app.archived_at === null),
      )
      return jsonResponse(apps)
    }
    if (method === 'POST' && path === '/v1/catalog/applications') {
      const body = (await request.json()) as Partial<Application>
      const app: Application = {
        id: this.id('app'),
        name: body.name ?? 'New application',
        description: body.description ?? null,
        project_id: body.project_id ?? 'telecom-modernization',
        archived_at: null,
        created_at: nowIso(),
        updated_at: nowIso(),
      }
      this.applications.unshift(app)
      return jsonResponse(app, 201)
    }
    const id = decodePart(path.split('/')[4])
    const index = this.applications.findIndex((item) => item.id === id)
    if (index === -1) return problemResponse(404, 'not_found', `Application ${id} was not found.`)
    const current = this.applications[index]!
    if (method === 'GET') return jsonResponse(current)
    if (method === 'PATCH') {
      const body = (await request.json()) as Partial<Application>
      this.applications[index] = {
        ...current,
        name: body.name ?? current.name,
        description: body.description ?? current.description,
        project_id: body.project_id ?? current.project_id,
        archived_at: body.archived_at ?? current.archived_at,
        updated_at: nowIso(),
      }
      return jsonResponse(this.applications[index])
    }
    if (method === 'DELETE') {
      this.applications.splice(index, 1)
      return emptyResponse()
    }
    if (method === 'POST' && path.endsWith('/archive')) {
      current.archived_at = nowIso()
      return jsonResponse(this.applications[index])
    }
    if (method === 'POST' && path.endsWith('/unarchive')) {
      current.archived_at = null
      return jsonResponse(this.applications[index])
    }
    return problemResponse(501, 'dummy_handler_missing', `Dummy data has no handler for ${method} ${path}.`)
  }

  private async handleEnvironments(method: string, path: string, request: Request, params: URLSearchParams): Promise<Response> {
    if (method === 'GET' && path === '/v1/catalog/environments') {
      const application = params.get('application')
      return jsonResponse(this.environments.filter((env) => !application || env.application_id === application))
    }
    if (method === 'POST' && path === '/v1/catalog/environments') {
      const body = (await request.json()) as Partial<Environment>
      const env = this.environment(this.id('env'), body.application_id ?? 'app-digital-device', body.name ?? 'new-env', body.kind ?? 'k8s', body.base_url ?? null, false)
      env.hosts = (body.hosts ?? []).map((host, index) => ({
        id: this.id(`host-${index}`),
        hostname: host.hostname,
        role: host.role ?? null,
      }))
      env.options = body.options ?? {}
      this.environments.unshift(env)
      this.inventories.set(env.id, { environment_id: env.id, snapshot: null })
      return jsonResponse(env, 201)
    }
    const id = decodePart(path.split('/')[4])
    const index = this.environments.findIndex((item) => item.id === id)
    if (index === -1) return problemResponse(404, 'not_found', `Environment ${id} was not found.`)
    const current = this.environments[index]!
    if (method === 'GET') return jsonResponse(current)
    if (method === 'PATCH') {
      const body = (await request.json()) as Partial<Environment>
      this.environments[index] = {
        ...current,
        application_id: body.application_id ?? current.application_id,
        base_url: body.base_url ?? current.base_url,
        kind: body.kind ?? current.kind,
        name: body.name ?? current.name,
        options: body.options ?? current.options,
        last_snapshot: body.last_snapshot ?? current.last_snapshot,
        hosts: body.hosts
          ? body.hosts.map((host, hostIndex) => ({
              id: this.id(`host-${hostIndex}`),
              hostname: host.hostname,
              role: host.role ?? null,
            }))
          : current.hosts,
        updated_at: nowIso(),
      }
      return jsonResponse(this.environments[index])
    }
    if (method === 'DELETE') {
      this.environments.splice(index, 1)
      this.inventories.delete(id)
      return emptyResponse()
    }
    return problemResponse(501, 'dummy_handler_missing', `Dummy data has no handler for ${method} ${path}.`)
  }

  private handleInventory(method: string, path: string): Response {
    const parts = path.split('/')
    const environmentId = decodePart(parts[4])
    if (method === 'GET') return jsonResponse(this.inventories.get(environmentId) ?? { environment_id: environmentId, snapshot: null })
    if (method === 'POST' && parts[5] === 'rescan') {
      const fresh: InventoryView = {
        environment_id: environmentId,
        snapshot: {
          scanned_at: nowIso(),
          stale: false,
          services: [
            { name: 'acc-web', image: 'registry.example.test/acc-web:4.8.2', replicas: 4 },
            { name: 'mulesoft-add-line-api', image: 'registry.example.test/order-mgmt:7.14.1', replicas: 3 },
            { name: 'mq-fulfillment-consumer', image: 'registry.example.test/mq-consumer:3.6.0', replicas: 4 },
          ],
        },
      }
      this.inventories.set(environmentId, fresh)
      return jsonResponse(fresh)
    }
    return problemResponse(501, 'dummy_handler_missing', `Dummy data has no handler for ${method} ${path}.`)
  }

  private async handleWorkTracking(method: string, path: string, request: Request, params: URLSearchParams): Promise<Response> {
    const input = method === 'GET' ? null : await request.clone().json().catch(() => null)
    if (params.get('connection_id') === 'conn-ado' || params.get('provider')?.toLowerCase() === 'ado' || input?.connection_id === 'conn-ado' || input?.provider?.toLowerCase() === 'ado' || input?.query?.provider?.toLowerCase() === 'ado') {
      return problemResponse(422, 'retired_provider', 'Azure DevOps has been retired. Use Jira Align for user stories.')
    }
    if (method === 'GET' && path === '/v1/work-tracking/binding') {
      return jsonResponse(this.workTrackingBinding(params.get('connection_id')))
    }
    if (method === 'POST' && path === '/v1/work-tracking/query/translate') {
      const body = (await request.json()) as { text?: string; connection_id?: string }
      const text = body.text ?? 'open load test bugs'
      const lowered = text.toLowerCase()
      const provider = lowered.includes('align') ? 'jira_align' : body.connection_id ? undefined : 'jira'
      const binding = provider ? this.workTrackingBinding(undefined, provider) : this.workTrackingBinding(body.connection_id)
      const jiraQuery = `project = PERF AND text ~ "${text}"`
      const isAccScope = /acc|add[ -]a[ -]line|flagship|release scope/.test(lowered)
      const queryByProvider: Record<string, string> = {
        jira: jiraQuery,
        jira_align: isAccScope
          ? FLAGSHIP_ALIGN_QUERY
          : `programId eq 42 and contains(title,'${text}')`,
      }
      return jsonResponse({
        ...binding,
        query: queryByProvider[binding.provider] ?? jiraQuery,
        confidence: 0.86,
      } satisfies ResolvedTranslatedQuery)
    }
    if (method === 'POST' && path === '/v1/work-tracking/query/execute') {
      const body = (await request.json()) as { connection_id?: string; limit?: number; offset?: number; query?: TranslatedQuery }
      const binding = this.workTrackingBinding(body.connection_id, body.query?.provider)
      if (body.query && body.query.provider.toLowerCase() !== binding.provider) {
        return problemResponse(
          409,
          'work_tracking_provider_mismatch',
          'The query provider does not match the selected work-tracking connection.',
        )
      }
      let rows = this.workItems.filter((item) => this.workItemProviders.get(item.key) === binding.provider)
      const queryText = body.query?.query?.toLowerCase() ?? ''
      if (binding.provider === 'jira_align') {
        const titleContains = /contains\(\s*title\s*,\s*['"]([^'"]+)['"]\s*\)/i.exec(queryText)?.[1]
        if (titleContains) rows = rows.filter((item) => item.title.toLowerCase().includes(titleContains))
        const itemTypes = /itemtype\s+in\s*\(([^)]+)\)/i.exec(queryText)?.[1]
          ?.split(',')
          .map((value) => value.replaceAll(/["']/g, '').trim().toLowerCase())
          .filter(Boolean)
        if (itemTypes?.length) rows = rows.filter((item) => itemTypes.includes(item.kind.toLowerCase()))
        if (/state\s+ne\s+['"]accepted['"]/i.test(queryText)) {
          rows = rows.filter((item) => item.status.toLowerCase() !== 'accepted')
        }
        if (/state\s+eq\s+['"]open['"]/i.test(queryText)) {
          rows = rows.filter((item) => item.status.toLowerCase() === 'open')
        }
      } else if (queryText.includes('status') && queryText.includes('open')) {
        rows = rows.filter((item) => !['done', 'accepted'].includes(item.status.toLowerCase()))
      }
      return jsonResponse(this.workItemPage(rows, body.limit ?? 25, body.offset ?? 0, binding))
    }
    if (method === 'GET' && path === '/v1/work-tracking/saved-queries') {
      const project = params.get('project')
      const provider = params.get('provider')
      const rows = this.savedQueries.filter((item) => (!project || item.project_id === project) && (!provider || item.provider === provider))
      const page = paginate(rows, params)
      return jsonResponse({ items: page.items, limit: page.limit, offset: page.offset })
    }
    if (method === 'POST' && path === '/v1/work-tracking/saved-queries') {
      const body = (await request.json()) as SavedQueryCreate
      const saved: SavedQuery = { id: this.id('sq'), created_at: nowIso(), updated_at: nowIso(), created_by: 'dev', description: body.description ?? null, name: body.name, provider: body.provider, query: body.query, project_id: body.project_id ?? null, connection_id: body.connection_id ?? null }
      this.savedQueries.unshift(saved)
      return jsonResponse(saved, 201)
    }
    const savedMatch = /^\/v1\/work-tracking\/saved-queries\/([^/]+)$/.exec(path)
    if (savedMatch) return this.handleSavedQuery(method, decodePart(savedMatch[1]), request)
    if (method === 'POST' && path === '/v1/work-tracking/items') {
      const body = (await request.json()) as WorkItemDraft
      const binding = this.workTrackingBinding(params.get('connection_id'))
      if (binding.provider !== 'jira') return problemResponse(409, 'demo_ticket_destination', 'Offline demo ticket creation is supported through Jira Cloud only.')
      const jiraNumbers = this.workItems.flatMap((item) => /^PERF-(\d+)$/.exec(item.key)?.[1] ? [Number(/^PERF-(\d+)$/.exec(item.key)![1])] : [])
      const key = `PERF-${Math.max(248, ...jiraNumbers) + 1}`
      const item: WorkItem = { key, title: body.title, description: body.description ?? '', kind: body.kind ?? 'story', status: 'open', url: null }
      this.workItems.unshift(item)
      this.workItemProviders.set(key, 'jira')
      return jsonResponse({
        ...item,
        ...binding,
      } satisfies ResolvedWorkItem, 201)
    }
    const itemMatch = /^\/v1\/work-tracking\/items\/([^/]+)(?:\/enrich)?$/.exec(path)
    if (itemMatch) {
      const key = decodePart(itemMatch[1])
      const item = this.workItems.find((row) => row.key === key)
      if (!item) return problemResponse(404, 'not_found', `Work item ${key} was not found.`)
      const binding = this.workTrackingBinding(params.get('connection_id'))
      if (this.workItemProviders.get(key) !== binding.provider) return problemResponse(404, 'not_found', `Work item ${key} is not available from ${binding.provider}.`)
      const resolved = {
        ...item,
        ...binding,
      } satisfies ResolvedWorkItem
      if (method === 'GET') return jsonResponse(resolved)
      if (method === 'POST' && path.endsWith('/enrich')) {
        const body = (await request.json()) as { comment?: string | null }
        item.description = [item.description, body.comment].filter(Boolean).join('\n\n')
        return jsonResponse({
          ...item,
          ...binding,
        } satisfies ResolvedWorkItem)
      }
    }
    return problemResponse(501, 'dummy_handler_missing', `Dummy data has no handler for ${method} ${path}.`)
  }

  private async handleSavedQuery(method: string, id: string, request: Request): Promise<Response> {
    const index = this.savedQueries.findIndex((item) => item.id === id)
    if (index === -1) return problemResponse(404, 'not_found', `Saved query ${id} was not found.`)
    const current = this.savedQueries[index]!
    if (method === 'GET') return jsonResponse(current)
    if (method === 'PATCH') {
      const body = (await request.json()) as SavedQueryUpdate
      this.savedQueries[index] = {
        ...current,
        name: body.name ?? current.name,
        description: body.description ?? current.description,
        provider: body.provider ?? current.provider,
        query: body.query ?? current.query,
        connection_id:
          body.connection_id === undefined ? current.connection_id : body.connection_id,
        updated_at: nowIso(),
      }
      return jsonResponse(this.savedQueries[index])
    }
    if (method === 'DELETE') {
      this.savedQueries.splice(index, 1)
      return emptyResponse()
    }
    return problemResponse(501, 'dummy_handler_missing', `Dummy data has no handler for saved query ${method}.`)
  }

  private async handleApexLoad(method: string, path: string, request: Request, _params: URLSearchParams): Promise<Response> {
    void _params
    const idempotencyHeader = request.headers.get('Idempotency-Key')
    const idempotencyKey = idempotencyHeader ? `${method}:${path}:${idempotencyHeader}` : null
    const replay = idempotencyKey ? this.apexIdempotency.get(idempotencyKey) : null
    if (replay) return jsonResponse(replay.body, replay.status)
    const remember = (body: Record<string, unknown>, status = 200): Response => {
      if (idempotencyKey) this.apexIdempotency.set(idempotencyKey, { status, body: clone(body) })
      return jsonResponse(body, status)
    }
    const connection = { id: 'conn-apex-load', name: 'APEX Load PERFHYPER2 Pool', project_id: 'telecom-modernization' }
    if (method === 'GET' && path === '/v1/apex-load/connections') return jsonResponse({ items: [connection], truncated: false })
    if (method === 'GET' && path.endsWith('/capabilities')) {
      return jsonResponse({
        templates: [
          { id: 'esim-journey', name: 'ACC add-a-line journey' },
          { id: 'mq-recovery', name: 'IBM MQ recovery verification' },
        ],
        protocols: ['jmeter', 'vugen'],
        scheduling: true,
        approvals: true,
      })
    }
    if (method === 'GET' && path.endsWith('/scenarios')) return jsonResponse({ items: this.apexScenarios, truncated: false })
    if (method === 'GET' && path.endsWith('/runs')) {
      return jsonResponse({
        items: [
          { id: 'native-run-2603', name: 'ACC add-a-line certification 26.3', status: 'COMPLETED', console_url: 'https://apex-load.example.test/#/test/native-run-2603' },
          { id: 'native-run-active', name: 'ACC add-a-line active ramp', status: 'RUNNING', console_url: 'https://apex-load.example.test/#/test/native-run-active' },
          { id: 'native-run-baseline', name: 'ACC add-a-line baseline 26.2.2', status: 'COMPLETED', console_url: 'https://apex-load.example.test/#/test/native-run-baseline' },
          { id: 'native-run-recovery', name: 'MQ recovery rehearsal', status: 'FAILED', console_url: 'https://apex-load.example.test/#/test/native-run-recovery' },
        ],
        truncated: false,
      })
    }
    if (method === 'GET' && path.endsWith('/mirrored-runs')) {
      return jsonResponse({ items: ['run-active', 'run-baseline-a', 'run-baseline-b', 'run-engine-recovery'].map((thread_id, index) => ({ id: ['native-run-active', 'native-run-2603', 'native-run-baseline', 'native-run-recovery'][index], pipeline_thread_id: thread_id, pipeline_status: 'launched', status: index === 0 ? 'RUNNING' : index === 3 ? 'FAILED' : 'COMPLETE', terminal: index !== 0, connection_version: 'demo-v1', last_seen_at: isoMinutesAgo(0, this.activationTime) })), limit: 25, offset: 0 })
    }
    if (method === 'GET' && path.endsWith('/schedule-health')) return jsonResponse({ details: { status: 'healthy', scheduler_lag_seconds: 0, next_occurrence_at: isoMinutesAgo(-240, this.activationTime) } })
    if (method === 'GET' && path.endsWith('/calendar')) return jsonResponse({ details: { windows: [{ id: 'window-1', name: 'PERFHYPER2 reserved window', starts_at: isoMinutesAgo(-240, this.activationTime), ends_at: isoMinutesAgo(-330, this.activationTime) }] } })
    if (method === 'GET' && path.endsWith('/occurrences')) return jsonResponse({ items: this.apexOccurrences.map((item) => ({ ...item, scheduled_for: isoMinutesAgo(Number(item['scheduled_offset_minutes'] ?? 0), this.activationTime) })), truncated: false })
    if (method === 'GET' && /\/scenarios\/[^/]+\/schedules$/.test(path)) {
      const scenarioId = decodePart(path.split('/').at(-2) ?? '')
      return jsonResponse({ items: this.apexSchedules.filter((item) => item['scenario_id'] === scenarioId), truncated: false })
    }
    if (method === 'GET' && path.endsWith('/metrics')) return jsonResponse({ details: { tps_avg: 84.7, tps_peak: 91.2, p95_ms: 3_650, error_rate: 0.028, vusers_peak: 300, mq_depth_peak: 18_420, hpa_pods_peak: 8 } })
    if (method === 'GET' && path.endsWith('/evidence')) return jsonResponse({ details: { signature: 'demo-sha256-a8248e', packets: this.evidence.slice(4, 9), immutable: true } })
    if (method === 'POST' && path.endsWith('/validation')) return jsonResponse({ details: { valid: true, warnings: [], checks: ['correlation', 'synthetic-data', 'stepped-ramp', 'idempotency'] } })
    if (method === 'POST' && path.endsWith('/sizing')) return jsonResponse({ details: { recommended_generators: 3, estimated_vusers: 300, estimated_tps: 85, headroom_percent: 25 } })
    if (method === 'POST' && /\/scenarios$/.test(path)) {
      const body = (await request.json()) as { payload?: Record<string, unknown> }
      const created = { id: this.id('scenario'), name: String(body.payload?.['name'] ?? 'New telecom scenario'), status: 'draft', ...body.payload }
      this.apexScenarios.unshift(created)
      return remember({ ...created, details: created }, 201)
    }
    if (method === 'POST' && /\/scenarios\/[^/]+\/schedules$/.test(path)) {
      const body = (await request.json()) as { payload?: Record<string, unknown> }
      const scenarioId = decodePart(path.split('/').at(-2) ?? '')
      const created = { id: this.id('schedule'), scenario_id: scenarioId, name: String(body.payload?.['name'] ?? 'New certification schedule'), status: 'pending_approval', approval_id: this.id('approval'), ...body.payload }
      this.apexSchedules.unshift(created)
      return remember({ ...created, details: created }, 201)
    }
    if (method === 'POST' && /\/approvals\/[^/]+$/.test(path)) {
      const body = (await request.json()) as { approved?: boolean }
      const approvalId = decodePart(path.split('/').at(-1) ?? '')
      const approved = Boolean(body.approved)
      for (const schedule of this.apexSchedules) {
        if (schedule['approval_id'] === approvalId) schedule['status'] = approved ? 'enabled' : 'rejected'
      }
      for (const occurrence of this.apexOccurrences) {
        if (occurrence['approval_id'] === approvalId) occurrence['status'] = approved ? 'approved' : 'rejected'
      }
      return remember({ details: { approved, decided_at: nowIso(), decided_by: 'Demo Presenter' } })
    }
    return problemResponse(501, 'demo_handler_missing', `Offline demo has no APEX Load handler for ${method} ${path}.`)
  }

  private async handleContext(method: string, path: string, request: Request, params: URLSearchParams): Promise<Response> {
    if (method === 'GET' && path === '/v1/context/evidence') {
      const project = params.get('project')
      const threadId = params.get('thread_id')
      return jsonResponse(
        this.evidence.filter(
          (item) => (!threadId || item.thread_id === threadId) && (!project || project === TELECOM_PROJECT_ID),
        ),
      )
    }
    if (method === 'POST' && path === '/v1/context/summaries') {
      const body = (await request.json()) as ContextSummaryRequest
      const threadId = this.id('context-thread')
      const runId = this.id('ctx-run')
      const subject = body.subject.trim()
      const lowered = subject.toLowerCase()
      const focus = lowered.includes('mq') || lowered.includes('queue')
        ? 'IBM MQ backlog risk is driven by two active consumers rather than the approved four; use a bounded ramp hold during recovery.'
        : lowered.includes('sla') || lowered.includes('release')
          ? 'Release certification is conditional because BSSe create-order p95 reached 3,650ms against the 3,000ms SLA.'
          : 'Peak risks are BSSe p95 latency, IBM MQ consumer backlog, retry idempotency, BSSe eligibility reads, and AKS HPA response.'
      const summary = `${subject}: Jira Align feature:583 and story:9012 define the ACC add-a-line certification scope. ${focus} This summary was generated by the bundled deterministic offline agent.`
      this.contextSummaryJobs.set(runId, { runId, threadId, subject, summary })
      return jsonResponse({ run_id: runId, stream_url: `/threads/${threadId}/runs/${runId}/stream`, subject }, 202)
    }
    return problemResponse(501, 'dummy_handler_missing', `Dummy data has no handler for ${method} ${path}.`)
  }

  private async handleDocuments(method: string, path: string, request: Request, params: URLSearchParams): Promise<Response> {
    if (method === 'GET' && path === '/v1/documents') {
      const project = params.get('project')
      const q = params.get('q')
      const rows = this.documents.filter((doc) => (!project || doc.project_id === project) && matchesText([doc.name, doc.summary], q))
      const page = paginate(rows, params)
      return jsonResponse({ items: page.items, limit: page.limit, offset: page.offset })
    }
    if (method === 'POST' && path === '/v1/documents') {
      const form = await request.formData()
      const file = form.get('file')
      const name = file instanceof File ? file.name : 'uploaded-document.txt'
      const size = file instanceof File ? file.size : 0
      const doc = this.document(this.id('doc'), name, file instanceof File ? file.type || 'application/octet-stream' : 'text/plain', size, String(form.get('summary') ?? 'Uploaded in offline demo mode.'))
      doc.project_id = String(form.get('project_id') ?? 'telecom-modernization')
      this.documents.unshift(doc)
      return jsonResponse(doc, 201)
    }
    const id = decodePart(path.split('/')[3])
    const index = this.documents.findIndex((doc) => doc.id === id)
    if (index === -1) return problemResponse(404, 'not_found', `Document ${id} was not found.`)
    if (method === 'GET') return jsonResponse(this.documents[index])
    if (method === 'DELETE') {
      this.documents.splice(index, 1)
      return emptyResponse()
    }
    return problemResponse(501, 'dummy_handler_missing', `Dummy data has no handler for ${method} ${path}.`)
  }

  private async handleDrafts(method: string, path: string, request: Request, params: URLSearchParams): Promise<Response> {
    if (method === 'GET' && path === '/v1/drafts') {
      const project = params.get('project')
      return jsonResponse(this.drafts.filter((draft) => !project || draft.project_id === project))
    }
    if (method === 'POST' && path === '/v1/drafts') {
      const body = (await request.json()) as Partial<DraftRead>
      const draft = this.draft(this.id('draft'), body.title ?? 'Untitled draft', body.project_id ?? null, body.payload ?? {})
      this.drafts.unshift(draft)
      return jsonResponse(draft, 201)
    }
    const id = decodePart(path.split('/')[3])
    const index = this.drafts.findIndex((draft) => draft.id === id)
    if (index === -1) return problemResponse(404, 'not_found', `Draft ${id} was not found.`)
    const current = this.drafts[index]!
    if (method === 'GET') return jsonResponse(current)
    if (method === 'PUT') {
      const body = (await request.json()) as Partial<DraftRead>
      this.drafts[index] = {
        ...current,
        title: body.title ?? current.title,
        payload: body.payload ?? current.payload,
        updated_at: nowIso(),
      }
      return jsonResponse(this.drafts[index])
    }
    if (method === 'DELETE') {
      this.drafts.splice(index, 1)
      return emptyResponse()
    }
    return problemResponse(501, 'dummy_handler_missing', `Dummy data has no handler for ${method} ${path}.`)
  }

  private async handlePrompts(method: string, path: string, request: Request, params: URLSearchParams): Promise<Response> {
    if (method === 'GET' && path === '/v1/prompts') {
      const ns = params.get('namespace')
      const q = params.get('q')
      const includeArchived = params.get('include_archived') === 'true'
      const rows = this.prompts
        .filter((prompt) => (!ns || prompt.namespace === ns) && (includeArchived || !prompt.archived_at) && matchesText([prompt.namespace, prompt.key, prompt.description], q))
        .map((prompt) => this.promptSummary(prompt))
      return jsonResponse(rows)
    }
    if (method === 'POST' && path === '/v1/prompts') {
      const body = (await request.json()) as { namespace: string; key: string; content: string; description?: string | null; note?: string | null }
      const version: PromptVersionDetail = { id: this.id('v'), version: 1, content: body.content, note: body.note ?? null, created_by: 'dev', created_at: nowIso(), parent_version_id: null }
      const prompt: PromptRecord = { id: this.id('prompt'), namespace: body.namespace, key: body.key, description: body.description ?? null, content: body.content, note: body.note ?? null, active_version: { id: version.id, version: 1 }, updated_at: nowIso(), archived_at: null, versions: [version] }
      this.prompts.unshift(prompt)
      return jsonResponse(this.promptDetail(prompt), 201)
    }
    const parts = path.split('/')
    const id = decodePart(parts[3])
    const prompt = this.prompts.find((item) => item.id === id)
    if (!prompt) return problemResponse(404, 'not_found', `Prompt ${id} was not found.`)
    if (method === 'GET' && parts.length === 4) return jsonResponse(this.promptDetail(prompt))
    if (method === 'POST' && parts[4] === 'archive') {
      prompt.archived_at = nowIso()
      return jsonResponse(this.promptSummary(prompt))
    }
    if (method === 'POST' && parts[4] === 'unarchive') {
      prompt.archived_at = null
      return jsonResponse(this.promptSummary(prompt))
    }
    if (method === 'POST' && parts[4] === 'rollback') {
      const body = (await request.json()) as { version_id?: string }
      const version = prompt.versions.find((item) => item.id === body.version_id) ?? prompt.versions[0]!
      prompt.active_version = { id: version.id, version: version.version }
      prompt.content = version.content
      prompt.updated_at = nowIso()
      return jsonResponse(this.promptDetail(prompt))
    }
    if (method === 'POST' && parts[4] === 'test') {
      const body = (await request.json()) as {
        sample_input?: Record<string, unknown>
        project_id?: string
        app_id?: string
        version_id?: string
        content?: string
      }
      const thread = this.createThread({ metadata: { project_id: TELECOM_PROJECT_ID, kind: 'prompt_playground', prompt_id: prompt.id } })
      const run = this.createRun(thread.thread_id, 'pipeline', { input: { title: `Prompt playground: ${prompt.key}`, request: `Render a deterministic offline response for ${prompt.key}.` }, config: { configurable: { phases: [prompt.namespace === 'phase' ? prompt.key.split('/')[0] : 'story_analysis'] } } })
      const detail = this.pipelineDetails.get(thread.thread_id)!
      detail.project_id = body.project_id ?? TELECOM_PROJECT_ID
      detail.app_id = body.app_id || detail.app_id
      detail.values = {
        ...(detail.values ?? {}),
        prompt_id: prompt.id,
        prompt_key: prompt.key,
        prompt_source: body.content
          ? 'adhoc'
          : body.version_id ?? prompt.active_version?.id ?? prompt.versions.at(-1)?.id ?? 'active',
        sample_input: body.sample_input ?? {},
      }
      this.runStages.set(thread.thread_id, 'prompt_playground')
      this.persistState()
      return jsonResponse({ run_id: run.run_id, thread_id: thread.thread_id }, 202)
    }
    if (method === 'GET' && parts[4] === 'versions' && parts.length === 5) {
      return jsonResponse(
        prompt.versions.map(({ id, version, note, created_by, created_at, parent_version_id }) => ({
          id,
          version,
          note,
          created_by,
          created_at,
          parent_version_id,
        })),
      )
    }
    if (method === 'POST' && parts[4] === 'versions') {
      const body = (await request.json()) as { content: string; note?: string | null }
      const parent = prompt.versions[prompt.versions.length - 1]
      const version: PromptVersionDetail = { id: this.id('v'), version: prompt.versions.length + 1, content: body.content, note: body.note ?? null, created_by: 'dev', created_at: nowIso(), parent_version_id: parent?.id ?? null }
      prompt.versions.push(version)
      prompt.content = version.content
      prompt.active_version = { id: version.id, version: version.version }
      prompt.updated_at = nowIso()
      return jsonResponse(version, 201)
    }
    if (method === 'GET' && parts[4] === 'versions') {
      const version = prompt.versions.find((item) => item.id === decodePart(parts[5]) || String(item.version) === decodePart(parts[5]))
      return version ? jsonResponse(version) : problemResponse(404, 'not_found', `Prompt version was not found.`)
    }
    return problemResponse(501, 'dummy_handler_missing', `Dummy data has no handler for ${method} ${path}.`)
  }

  private async handleConnections(method: string, path: string, request: Request, params: URLSearchParams): Promise<Response> {
    if (method === 'GET' && path === '/v1/admin/connections') {
      const kind = params.get('kind')
      const project = params.get('project')
      return jsonResponse(this.connections.filter((conn) => (!kind || conn.kind === kind) && (!project || conn.project_id === project)))
    }
    if (method === 'POST' && path === '/v1/admin/connections') {
      const body = (await request.json()) as ConnectionCreate
      const conn = this.connection(this.id('conn'), body.kind, body.provider, body.name, body.project_id ?? null, body.base_url ?? null, true, body.options ?? {}, body.secret_ref ?? null)
      this.connections.unshift(conn)
      return jsonResponse(conn, 201)
    }
    const parts = path.split('/')
    const id = decodePart(parts[4])
    const index = this.connections.findIndex((conn) => conn.id === id)
    if (index === -1) return problemResponse(404, 'not_found', `Connection ${id} was not found.`)
    const conn = this.connections[index]!
    if (method === 'GET' && parts.length === 5) return jsonResponse(conn)
    if (method === 'PATCH') {
      const body = (await request.json()) as ConnectionUpdate
      this.connections[index] = {
        ...conn,
        base_url: body.base_url ?? conn.base_url,
        name: body.name ?? conn.name,
        options: body.options ?? conn.options,
        project_id: body.project_id ?? conn.project_id,
        provider: body.provider ?? conn.provider,
        secret_ref: body.secret_ref ?? conn.secret_ref,
        updated_at: nowIso(),
      }
      return jsonResponse(this.connections[index])
    }
    if (method === 'DELETE') {
      this.connections.splice(index, 1)
      return emptyResponse()
    }
    if (method === 'POST' && parts[5] === 'enable') {
      conn.enabled = true
      conn.updated_at = nowIso()
      return jsonResponse(conn)
    }
    if (method === 'POST' && parts[5] === 'disable') {
      conn.enabled = false
      conn.updated_at = nowIso()
      return jsonResponse(conn)
    }
    if (method === 'GET' && parts[5] === 'host-mappings') return jsonResponse(this.hostMappings.get(id) ?? [])
    if (method === 'PUT' && parts[5] === 'host-mappings') {
      const body = (await request.json()) as HostMappingIn[]
      const mappings = body.map((item) => ({ id: this.id('map'), pattern: item.pattern, target: item.target, enabled: item.enabled }))
      this.hostMappings.set(id, mappings)
      return jsonResponse(mappings)
    }
    if (method === 'POST' && parts[5] === 'test') {
      const probe: ProbeResult = { ok: conn.enabled, detail: conn.enabled ? 'Offline simulated probe succeeded.' : 'Connection is disabled in the demo dataset.', latency_ms: conn.enabled ? 42 : 0 }
      return jsonResponse(probe)
    }
    return problemResponse(501, 'dummy_handler_missing', `Dummy data has no handler for ${method} ${path}.`)
  }

  private async handleConsumers(method: string, path: string, request: Request): Promise<Response> {
    if (method === 'GET' && path === '/v1/admin/consumers') return jsonResponse(this.consumers)
    if (method === 'POST' && path === '/v1/admin/consumers') {
      const body = (await request.json()) as ConsumerCreate
      const created = this.consumerCreated(this.id('cons'), body.name, body.role, body.consumer_type, true, this.fingerprint())
      this.consumers.unshift(({ ...created, api_key: undefined }) as Consumer)
      return jsonResponse(created, 201)
    }
    const parts = path.split('/')
    const id = decodePart(parts[4])
    const index = this.consumers.findIndex((consumer) => consumer.id === id)
    if (index === -1) return problemResponse(404, 'not_found', `Consumer ${id} was not found.`)
    const current = this.consumers[index]!
    if (method === 'GET') return jsonResponse(current)
    if (method === 'PATCH') {
      const body = (await request.json()) as ConsumerUpdate
      this.consumers[index] = {
        ...current,
        enabled: body.enabled ?? current.enabled,
        name: body.name ?? current.name,
        role: body.role ?? current.role,
        scopes: body.scopes ?? current.scopes,
      }
      return jsonResponse(this.consumers[index])
    }
    if (method === 'DELETE') {
      this.consumers.splice(index, 1)
      return emptyResponse()
    }
    if (method === 'POST' && parts[5] === 'rotate') {
      const rotated = this.consumerCreated(current.id, current.name, current.role, current.consumer_type, current.enabled, this.fingerprint())
      this.consumers[index] = ({ ...rotated, api_key: undefined }) as Consumer
      return jsonResponse(rotated)
    }
    return problemResponse(501, 'dummy_handler_missing', `Dummy data has no handler for ${method} ${path}.`)
  }

  private environment(id: string, applicationId: string, name: string, kind: string, baseUrl: string | null, stale: boolean): Environment {
    return {
      id,
      application_id: applicationId,
      name,
      kind,
      base_url: baseUrl,
      target_approved: baseUrl !== null,
      target_version: baseUrl === null ? 0 : 1,
      hosts: [
        { id: `${id}-host-1`, hostname: `${name}-node-1`, role: 'worker' },
        { id: `${id}-host-2`, hostname: `${name}-node-2`, role: name === 'production' ? 'api' : null },
      ],
      options: { namespace: `${applicationId}-${name}`, demo_assumptions: true, ...(id === 'env-perf' ? { dependencies: ACC_DEMO_DEPENDENCIES } : {}) },
      created_at: isoMinutesAgo(12_000, this.activationTime),
      updated_at: isoMinutesAgo(30, this.activationTime),
      last_snapshot: { scanned_at: isoMinutesAgo(stale ? 2_400 : 15, this.activationTime), service_count: stale ? 2 : 4 },
    }
  }

  private connection(id: string, kind: PortKind, provider: string, name: string, projectId: string | null, baseUrl: string | null, enabled: boolean, options: Record<string, unknown>, secretRef: string | null): Connection {
    return { id, kind, provider, name, project_id: projectId, base_url: baseUrl, enabled, options, secret_ref: secretRef, created_at: isoMinutesAgo(8_000, this.activationTime), updated_at: isoMinutesAgo(25, this.activationTime) }
  }

  private consumer(id: string, name: string, role: Consumer['role'], consumerType: Consumer['consumer_type'], enabled: boolean, fingerprint: string): Consumer {
    return { id, name, role, consumer_type: consumerType, enabled, scopes: [{ project_id: 'telecom-modernization', app_id: null }], created_at: isoMinutesAgo(9_000, this.activationTime), last_used_at: enabled ? isoMinutesAgo(12, this.activationTime) : null, key_fingerprint: fingerprint, rotation_count: 0 }
  }

  private consumerCreated(id: string, name: string, role: Consumer['role'], consumerType: Consumer['consumer_type'], enabled: boolean, fingerprint: string): ConsumerCreated {
    return { ...this.consumer(id, name, role, consumerType, enabled, fingerprint), api_key: `apex_demo_${fingerprint}` }
  }

  private document(id: string, name: string, mediaType: string, sizeBytes: number, summary: string): DocumentOut {
    return { id, name, media_type: mediaType, size_bytes: sizeBytes, artifact_key: `documents/${id}`, project_id: 'telecom-modernization', app_id: null, summary, uploaded_by: 'Demo Presenter', created_at: isoMinutesAgo(1_000, this.activationTime) }
  }

  private draft(id: string, title: string, projectId: string | null, payload: Record<string, unknown>): DraftRead {
    return { id, title, project_id: projectId, payload, created_by: 'dev', created_at: isoMinutesAgo(2_000, this.activationTime), updated_at: isoMinutesAgo(20, this.activationTime) }
  }

  private makePrompts(created: string, updated: string): PromptRecord[] {
    const phase = [...TELECOM_PHASE_PROMPTS, ...SCRIPT_VARIANT_PROMPTS].map((seed, index) =>
      this.prompt(`prompt-phase-${index + 1}`, 'phase', seed.key, seed.description, seed.content, created, updated, false, `adapted from ${seed.source}`),
    )
    return [
      ...phase,
      this.prompt('prompt-app-device', 'application', 'app-digital-device', 'ACC add-a-line requirements', APPLICATION_PROMPT_TEMPLATE + '\n' + ACC_DEMO_CONTEXT, created, updated, false, 'telecom demo application context'),
      this.prompt('prompt-app-order', 'application', 'app-order-fulfillment', 'Order fulfillment requirements', APPLICATION_PROMPT_TEMPLATE + '\nDemo application scope (confirm against supplied evidence): Validate BSSe submission, four MQ consumers, bounded backlog recovery, and duplicate-event handling.', created, updated, false, 'telecom demo application context'),
      this.prompt('prompt-app-usage', 'application', 'app-usage-billing', 'Usage and billing requirements', APPLICATION_PROMPT_TEMPLATE + '\nDemo application scope (confirm against supplied evidence): Validate existing account eligibility, line limits, resulting line counts, and charges in BSSe TEST without production account data.', created, updated, false, 'telecom demo application context'),
      this.prompt('prompt-old', 'experimental', 'legacy/release-report', 'Archived pre-telecom prompt', 'Legacy release report instructions retained only to demonstrate prompt archival.', created, updated, true, 'archived demo history'),
    ]
  }

  private prompt(id: string, namespace: string, key: string, description: string, content: string, created: string, updated: string, archived: boolean, sourceNote = 'telecom demo seed'): PromptRecord {
    const v1: PromptVersionDetail = { id: `${id}-v1`, version: 1, content, note: sourceNote, created_by: 'Demo Curator', created_at: created, parent_version_id: null }
    const v2: PromptVersionDetail = { id: `${id}-v2`, version: 2, content: `${content}\n\nCite the supplied evidence and clearly label assumptions.`, note: `${sourceNote}; active evidence-attribution revision`, created_by: 'Demo Curator', created_at: updated, parent_version_id: v1.id }
    return { id, namespace, key, description, content: v2.content, note: v2.note, active_version: { id: v2.id, version: 2 }, archived_at: archived ? isoMinutesAgo(300, this.activationTime) : null, updated_at: updated, versions: [v1, v2] }
  }

  private promptSummary(prompt: PromptRecord): PromptSummary {
    const { id, namespace, key, description, archived_at, active_version, updated_at } = prompt
    return { id, namespace, key, description, archived_at, active_version, updated_at }
  }

  private promptDetail(prompt: PromptRecord): PromptDetail {
    const { id, namespace, key, description, archived_at, active_version, updated_at, content, note } = prompt
    return { id, namespace, key, description, archived_at, active_version, updated_at, content, note }
  }

  private makeLogs(): LogEntry[] {
    const services = ['acc-web', 'mulesoft-add-line-api', 'mq-fulfillment-consumer', 'mulesoft-esb', 'apim-gateway', 'bsse-account-api', 'apex-worker']
    return Array.from({ length: 40 }, (_, index) => {
      const mqIncident = index >= 4 && index <= 9
      const omsIncident = index >= 10 && index <= 15
      return {
        at: isoMinutesAgo(2 + index * 3, this.activationTime),
        level: mqIncident ? 'WARN' : omsIncident ? 'ERROR' : index % 7 === 0 ? 'DEBUG' : 'INFO',
        service: services[index % services.length]!,
        message: mqIncident
          ? `IBM MQ fulfillment depth ${4_200 + index * 920}; ramp hold checkpoint`
          : omsIncident
            ? `BSSe create-order p95 ${3_100 + index * 55}ms exceeded 3000ms SLA`
            : `telecom demo checkpoint ${index + 1} correlated successfully`,
        fields: { thread_id: index < 20 ? 'run-conditional' : 'run-active', checkpoint: index + 1 },
      }
    })
  }

  private makeAssistants(created: string, updated: string): Assistant[] {
    return [
      this.assistant('pipeline', 'pipeline', 'Telecom system default', 'All seven phases with deterministic offline agents.', { project_id: 'telecom-modernization' }, { created_by: 'Demo Curator' }, 1, created, updated),
      this.assistant('asst-release', 'pipeline', 'ACC add-a-line Release Certification', 'Full gated release-candidate certification profile.', { project_id: 'telecom-modernization', app_id: 'app-digital-device', environment_id: 'env-perf', engine: 'apexload', limits: { poll_interval_s: 1 } }, { created_by: 'Demo Curator' }, 4, created, updated),
      this.assistant('asst-fast', 'pipeline', 'Telecom Fast Smoke', 'Short story, execution, and reporting path.', { project_id: 'telecom-modernization', phases: ['story_analysis', 'execution', 'reporting'] }, { created_by: 'Demo Curator' }, 2, created, updated),
    ]
  }

  private assistant(id: string, graphId: string, name: string, description: string | null, configurable: Record<string, unknown>, metadata: Record<string, unknown>, version: number, created: string, updated: string): Assistant {
    return { assistant_id: id, graph_id: graphId, name, description: description ?? undefined, config: { configurable }, context: {}, metadata, version, created_at: created, updated_at: updated }
  }

  private seedPipelines(): void {
    const rows: Array<[string, string, string, 'busy' | 'interrupted' | 'idle' | 'error', PhaseName, 'prompt' | 'phase' | null]> = [
      ['run-active', 'ACC add-a-line active ramp', 'telecom-modernization', 'busy', 'execution', null],
      ['run-prompt-review', 'PI 26.3 workload prompt review', 'telecom-modernization', 'interrupted', 'test_planning', 'prompt'],
      ['run-report-review', 'ACC add-a-line release approval', 'telecom-modernization', 'interrupted', 'reporting', 'phase'],
      ['run-baseline-a', 'ACC add-a-line baseline 26.2.1', 'telecom-modernization', 'idle', 'postmortem', null],
      ['run-baseline-b', 'ACC add-a-line baseline 26.2.2', 'telecom-modernization', 'idle', 'postmortem', null],
      ['run-conditional', 'BSSe and MQ release rehearsal', 'telecom-modernization', 'idle', 'postmortem', null],
      ['run-aborted', 'Shared environment contention rehearsal', 'telecom-modernization', 'error', 'execution', null],
      ['run-engine-recovery', 'APEX Load provisioning recovery', 'telecom-modernization', 'interrupted', 'execution', null],
    ]
    for (const [id, title, project, status, phase, gate] of rows) {
      const detail = this.pipelineDetail(id, title, project, status, phase)
      const state = detail.values as PipelineState
      if (id === 'run-baseline-a' || id === 'run-baseline-b') {
        const result = state.phase_results?.execution
        if (result?.test_summary) {
          result.test_summary.passed = true
          result.test_summary.kpis = { tps_avg: id === 'run-baseline-a' ? 81.2 : 82.4, p95_ms: id === 'run-baseline-a' ? 2410 : 2520, error_rate: id === 'run-baseline-a' ? 0.006 : 0.007, vusers_peak: 300 }
          result.test_summary.sla_breaches = []
          result.test_summary.notes = 'Comparable ACC add-a-line baseline passed all release thresholds.'
        }
      }
      if (status === 'idle' && state.phase_results) state.phase_results.postmortem = { status: 'succeeded', attempt: 1, summary: 'Test review completed. See execution results and reports for release risks.' }
      if (id === 'run-aborted') {
        state.run_aborted = true
        state.phase_results!.reporting = { phase: 'reporting', status: 'pending' }
        state.phase_results!.postmortem = { phase: 'postmortem', status: 'pending' }
      }
      if (gate === 'prompt') this.addPromptGate(detail)
      if (gate === 'phase') this.addPhaseGate(detail)
      if (id === 'run-engine-recovery') this.addEngineRetryGate(detail)
      this.pipelineDetails.set(id, detail)
      this.runs.set(id, [
        {
          run_id: `${id}-active`,
          thread_id: id,
          assistant_id: 'pipeline',
          created_at: detail.created_at ?? nowIso(),
          updated_at: detail.updated_at ?? nowIso(),
          status: status === 'busy' ? 'running' : status === 'interrupted' ? 'interrupted' : status === 'error' ? 'error' : 'success',
          metadata: {},
          multitask_strategy: 'reject',
        },
      ])
      if (status === 'busy') this.runStages.set(id, phase)
    }
  }

  private initializeFreshPipeline(detail: PipelineDetail, phase: PhaseName): void {
    const state = detail.values as PipelineState
    state.current_phase = phase
    state.phase_results = {
      [phase]: {
        phase,
        status: 'running',
        attempt: 1,
        started_at: nowIso(),
      },
    }
    state.artifacts = []
    state.dialogue = []
    state.engine_handle = null
    detail.current_phase = phase
    detail.phase_strip = makeStrip({ [phase]: { status: 'running', attempt: 1 } })
    detail.engine = null
    detail.values = state as Record<string, unknown>
  }

  private setPhaseState(
    detail: PipelineDetail,
    phase: PhaseName,
    status: 'pending' | 'running' | 'awaiting_prompt_review' | 'awaiting_output_review' | 'succeeded' | 'failed' | 'skipped' | 'aborted',
    attempt = 1,
    summary?: string,
  ): void {
    detail.current_phase = phase
    detail.phase_strip = detail.phase_strip.map((entry) =>
      entry.phase === phase ? { ...entry, status, attempt } : entry,
    )
    const state = detail.values as PipelineState
    const existing = state.phase_results?.[phase] ?? {}
    state.current_phase = phase
    state.phase_results = {
      ...(state.phase_results ?? {}),
      [phase]: {
        ...existing,
        phase,
        status,
        attempt,
        started_at: existing.started_at ?? nowIso(),
        ended_at: ['succeeded', 'failed', 'skipped', 'aborted'].includes(status) ? nowIso() : null,
        ...(summary ? { summary } : {}),
      },
    }
    detail.values = state as Record<string, unknown>
  }

  private pipelineDetail(id: string, title: string, projectId: string | undefined, status: 'busy' | 'interrupted' | 'idle' | 'error', phase: PhaseName): PipelineDetail {
    const state = makePipelineState(title, phase, status, this.activationTime)
    return {
      thread_id: id,
      title,
      project_id: projectId ?? 'telecom-modernization',
      app_id: 'app-digital-device',
      thread_status: status,
      current_phase: phase,
      phase_strip: makeStrip({
        story_analysis: { status: 'succeeded', attempt: 1 },
        test_planning: { status: phase === 'test_planning' && status === 'interrupted' ? 'awaiting_prompt_review' : 'succeeded', attempt: 1 },
        env_triage: { status: 'succeeded', attempt: 1 },
        script_scenario: { status: 'succeeded', attempt: 1 },
        execution: { status: status === 'busy' ? 'running' : status === 'error' ? 'failed' : 'succeeded', attempt: status === 'error' ? 2 : 1 },
        reporting: { status: phase === 'reporting' && status === 'interrupted' ? 'awaiting_output_review' : status === 'busy' ? 'pending' : 'succeeded', attempt: status === 'busy' ? null : 1 },
        postmortem: { status: status === 'error' ? 'running' : status === 'idle' ? 'succeeded' : 'pending', attempt: status === 'error' || status === 'idle' ? 1 : null },
      }),
      engine: { engine: 'apexload', external_run_id: `AL-${id.toUpperCase()}` },
      created_at: isoMinutesAgo(180, this.activationTime),
      updated_at: status === 'interrupted' ? isoMinutesAgo(24, this.activationTime) : isoMinutesAgo(status === 'busy' ? 2 : 45, this.activationTime),
      pending_gate: null,
      values: state as Record<string, unknown>,
      interrupts: [],
    }
  }

  private addPromptGate(detail: PipelineDetail, revised = false): void {
    const interruptId = revised ? this.id('int-prompt-revision') : 'int-prompt'
    detail.pending_gate = { interrupt_id: interruptId, kind: 'prompt_review', phase: 'test_planning' }
    detail.interrupts = [
      {
        interrupt_id: interruptId,
        kind: 'prompt_review',
        phase: 'test_planning',
        payload: {
          schema_version: 1,
          kind: 'prompt_review',
          phase: 'test_planning',
          prompt: {
            system: 'You are the test planning phase operator.',
            user: revised
              ? ACC_DEMO_TEST_PLAN + '\nReview emphasis: eligibility rejection and delayed activation.'
              : ACC_DEMO_TEST_PLAN,
            source: { origin: revised ? 'run_override' : 'catalog', ref: 'phase/test_planning/system@v2' },
          },
          context_packets: [{ id: 'ctx-1', source: 'jira_align', title: 'story:9012 acceptance criteria', summary: 'employee add-a-line, resilient BSSe fulfillment, and retry behavior under load.' }],
          tools: ['jira_align.query', 'documents.search'],
          editable: true,
          actions: ['approve', 'modify', 'skip_phase', 'abort'],
        },
      },
    ]
  }

  private addPhaseGate(detail: PipelineDetail, phase: PhaseName = 'reporting', revised = false): void {
    const interruptId = this.id(`int-${phase}`)
    const envSummary = revised
      ? 'PERFHYPER2 rescan is green: mulesoft-add-line-api now targets the PERFHYPER2 service endpoint and four IBM MQ consumers are available.'
      : 'Environment triage found mulesoft-add-line-api targeting a non-PERFHYPER2 BSSe test endpoint and only two IBM MQ consumers instead of four.'
    const executionSummary = 'Execution completed successfully. BSSe create-order p95 was 3,650ms against a 3,000ms SLA; IBM MQ depth peaked at 18,420 while AKS HPA scaled from four to eight pods.'
    const reportSummary = 'Executive and technical reports are ready. Release certification is conditional, and Jira Cloud defect PERF-248 will be created on approval.'
    const genericSummary = `The offline ${phase.replaceAll('_', ' ')} rerun completed and is ready for operator review.`
    const summary = phase === 'env_triage'
      ? envSummary
      : phase === 'execution'
        ? executionSummary
        : phase === 'reporting'
          ? reportSummary
          : genericSummary
    detail.pending_gate = { interrupt_id: interruptId, kind: 'phase_review', phase }
    detail.interrupts = [
      {
        interrupt_id: interruptId,
        kind: 'phase_review',
        phase,
        payload: {
          schema_version: 1,
          kind: 'phase_review',
          phase,
          summary,
          evidence: (detail.values?.['phase_results'] as Record<string, unknown> | undefined)?.[phase],
          result_preview: { summary, reasoning_digest: phase === 'env_triage' ? (revised ? 'Configuration correction and rescan passed.' : 'BSSe target and MQ consumer count require correction.') : 'Orchestration succeeded; the BSSe SLA is the release risk.' },
          artifacts: [{ id: phase === 'reporting' ? 'executive-report' : 'exec-report', kind: 'report', name: phase === 'reporting' ? 'ACC add-a-line-executive-report.md' : `${phase}-result.json` }],
          warnings: revised
            ? []
            : phase === 'env_triage'
              ? ['non-PERFHYPER2 BSSe test target detected.', 'Only two of four MQ consumers are active.']
              : phase === 'execution' || phase === 'reporting'
                ? ['BSSe p95 exceeds the 3,000ms release SLA.']
                : [],
          dialogue_tail: (detail.values?.['dialogue'] as unknown[]) ?? [],
          actions: ['approve', 'revise', 'discuss', 'abort'],
        },
      },
    ]
  }

  private appendDialogue(detail: PipelineDetail, phase: PhaseName, message: string): void {
    const response = deterministicAgentReply(phase, message)
    const dialogue = (detail.values?.['dialogue'] as unknown[]) ?? []
    detail.values = { ...(detail.values ?? {}), dialogue: [...dialogue, { id: this.id('dialogue-operator'), role: 'operator', phase, content: message, at: nowIso() }, { id: this.id('dialogue-agent'), role: 'agent', phase, content: response, at: nowIso(), model: 'offline-deterministic-template' }] }
  }

  private ensurePerf248(threadId: string): WorkItem {
    const findingKey = `${threadId}:reporting:oms-p95`
    const mappedKey = this.reportingTickets.get(findingKey)
    const mapped = mappedKey ? this.workItems.find((item) => item.key === mappedKey) : null
    if (mapped) return mapped
    const detail = this.pipelineDetails.get(threadId)
    const isFlagship = detail?.title === 'ACC Add-a-Line Peak Certification'
    const jiraNumbers = this.workItems.flatMap((item) => /^PERF-(\d+)$/.exec(item.key)?.[1] ? [Number(/^PERF-(\d+)$/.exec(item.key)![1])] : [])
    const ticketKey = isFlagship ? 'PERF-248' : `PERF-${Math.max(248, ...jiraNumbers) + 1}`
    const existing = this.workItems.find((item) => item.key === ticketKey)
    if (existing) {
      this.reportingTickets.set(findingKey, existing.key)
      return existing
    }
    const ticket: WorkItem = {
      key: ticketKey,
      title: '[Perf] BSSe p95 exceeds SLA during ACC add-a-line peak certification',
      kind: 'bug',
      status: 'Open',
      description: 'BSSe create-order p95 reached 3,650ms against the 3,000ms SLA. IBM MQ fulfillment depth peaked at 18,420 while AKS HPA scaled from four to eight pods. Orchestration completed successfully.',
      url: 'https://jira.example.test/browse/PERF-248',
      priority: 'Highest',
      severity: 'High',
      labels: ['perf-test', 'acc', 'add-line', 'bsse', 'release-blocker'],
      linked_keys: ['feature:583', 'story:9012'],
    }
    this.workItems.unshift(ticket)
    this.workItemProviders.set(ticket.key, 'jira')
    this.reportingTickets.set(findingKey, ticket.key)
    const artifactId = `${ticket.key}-jira-payload`
    this.artifacts.set(`reports/${artifactId}`, { mediaType: 'application/json', body: JSON.stringify({ key: ticket.key, fields: ticket, source_thread_id: threadId, idempotency_key: findingKey }, null, 2) })
    if (detail) {
      const state = detail.values as PipelineState
      const artifacts = state.artifacts ?? []
      if (!artifacts.some((artifact) => artifact.id === artifactId)) {
        state.artifacts = [
          ...artifacts,
          artifactRef(artifactId, `${ticket.key}-jira-payload.json`, 'jira_payload', 'application/json', `Created Jira Cloud defect ${ticket.key}.`),
        ]
        detail.values = state as Record<string, unknown>
      }
    }
    return ticket
  }

  private addEngineRetryGate(detail: PipelineDetail): void {
    detail.pending_gate = { interrupt_id: 'int-engine-retry', kind: 'engine_provision_retry', phase: 'execution' }
    detail.interrupts = [
      {
        interrupt_id: 'int-engine-retry',
        kind: 'engine_provision_retry',
        phase: 'execution',
        payload: {
          schema_version: 1,
          kind: 'engine_provision_retry',
          phase: 'execution',
          attempt: 1,
          thread_id: detail.thread_id,
          actions: ['retry'],
          error: 'APEX Load reservation timed out after remote acceptance.',
          message: 'Retry checkpointed provisioning reconciliation; the idempotency key prevents a duplicate native run.',
        },
      },
    ]
  }

  private seedArtifacts(): void {
    const rows: Array<[string, string, string, string]> = [
      ['exec-report', 'load-report.json', 'application/json', JSON.stringify({ engine: 'apexload', orchestration_status: 'succeeded', release_result: 'conditional_fail', kpis: { tps_avg: 84.7, p95_ms: 3650, error_rate: 0.028, vusers_peak: 300, mq_depth_peak: 18420 } }, null, 2)],
      ['execution-transcript', 'execution-transcript.txt', 'text/plain', 'APEX Load provisioned\nStepped ramp started\nRamp held for MQ backlog\nAKS HPA scaled 4 -> 8\nBSSe SLA finding recorded\n'],
      ['story-hierarchy', 'jira-align-hierarchy.json', 'application/json', JSON.stringify({ epic: 'epic:310', capability: 'capability:420', feature: 'feature:583', story: 'story:9012' }, null, 2)],
      ['scope-analysis', 'story-analysis.md', 'text/markdown', ACC_DEMO_CONTEXT],
      ['test-plan', 'test-plan.md', 'text/markdown', ACC_DEMO_TEST_PLAN],
      ['traffic-profile', 'traffic-profile.csv', 'text/csv', 'minute,vusers,target_tps\n0,0,0\n5,100,30\n10,200,60\n15,300,85\n'],
      ['env-scan-before', 'environment-before.json', 'application/json', JSON.stringify({ order_mgmt_endpoint: 'bsse-other-test.example.test', mq_consumers: 2, status: 'warning' }, null, 2)],
      ['env-scan-after', 'environment-after.json', 'application/json', JSON.stringify({ order_mgmt_endpoint: 'bsse.perfhyper2.example.test', mq_consumers: 4, status: 'green' }, null, 2)],
      ['config-diff', 'perf-configuration.diff', 'text/plain', '- BSSE_BASE_URL=https://bsse-other-test.example.test\n+ BSSE_BASE_URL=https://bsse.perfhyper2.example.test\n- MQ_CONSUMERS=2\n+ MQ_CONSUMERS=4\n'],
      ['jmeter-script', 'acc-add-line.jmx', 'application/xml', '<jmeterTestPlan version="1.2"><hashTree><TestPlan testname="ACC Add-a-Line"/><HeaderManager testname="Correlation and idempotency"/></hashTree></jmeterTestPlan>'],
      ['vugen-script', 'acc_add_line.c', 'text/x-c', 'Action(){ web_reg_save_param_json("ParamName=orderId", "QueryString=$.orderId", LAST); web_custom_request("add-line", "URL=https://acc.perfhyper2.example.test/add-line", LAST); return 0; }'],
      ['jmeter-validation', 'jmeter-validation.json', 'application/json', JSON.stringify({ valid: true, checks: ['correlation', 'synthetic-data', 'stepped-ramp', 'sla-assertions'] }, null, 2)],
      ['vugen-validation', 'vugen-validation.json', 'application/json', JSON.stringify({ valid: true, correlations: ['orderId', 'activationId'], transactions: 6 }, null, 2)],
      ['synthetic-data', 'synthetic-acc-add-line-data.csv', 'text/csv', ACC_DEMO_TEST_DATA],
      ['provision-request', 'apex-load-provision.json', 'application/json', JSON.stringify({ scenario: 'scenario-esim-peak', generators: 3, idempotency_key: 'demo-esim-2603' }, null, 2)],
      ['ramp-timeline', 'ramp-timeline.csv', 'text/csv', 'elapsed_s,event,mq_depth,hpa_pods\n0,provisioned,900,4\n420,ramp_hold,12100,6\n600,resume,8400,8\n1500,complete,2100,8\n'],
      ['mq-monitoring', 'mq-monitoring.md', 'text/markdown', '# IBM MQ timeline\n\nDepth peaked at 18,420. The bounded ramp hold allowed consumers to recover without aborting orchestration.'],
      ['hpa-monitoring', 'aks-hpa-timeline.json', 'application/json', JSON.stringify({ replicas: [{ at_s: 0, value: 4 }, { at_s: 390, value: 6 }, { at_s: 560, value: 8 }] }, null, 2)],
      ['oms-kpis', 'oms-kpis.csv', 'text/csv', 'metric,value,sla,result\ncreate_order_p95_ms,3650,3000,fail\nerror_rate,0.028,0.02,fail\ntps_avg,84.7,80,pass\n'],
      ['technical-report', 'ACC add-a-line-technical-report.md', 'text/markdown', '# Technical report\n\nOrchestration succeeded. BSSe latency and MQ backlog require remediation before production promotion.'],
      ['executive-report', 'ACC add-a-line-executive-report.md', 'text/markdown', '# Executive result\n\nConditional certification: target throughput achieved; BSSe p95 exceeded the release SLA.'],
      ['trend-comparison', 'baseline-comparison.csv', 'text/csv', 'run,p95_ms,error_rate,tps\n26.2.1,2410,0.006,81.2\n26.2.2,2520,0.007,82.4\n26.3,3650,0.028,84.7\n'],
      ['jira-draft', 'PERF-248-draft.json', 'application/json', JSON.stringify({ project: 'PERF', type: 'Bug', links: ['feature:583', 'story:9012'], finding: 'BSSe p95 SLA breach' }, null, 2)],
      ['evidence-manifest', 'evidence-manifest.json', 'application/json', JSON.stringify({ packets: 12, signed: true, source_types: ['jira_align', 'observability', 'documents'] }, null, 2)],
      ['postmortem', 'postmortem.md', 'text/markdown', '# Postmortem\n\nScale MQ consumers, tune AKS HPA stabilization, and optimize the BSSe create-order dependency path.'],
      ['remediation-plan', 'remediation-plan.csv', 'text/csv', 'owner,action,due\nBSSe team,Optimize create-order path,PI 26.3 I3\nPlatform,Scale MQ consumers to four,Before retest\nSRE,Tune HPA stabilization,Before retest\n'],
      ['agent-dialogue', 'agent-dialogue.txt', 'text/plain', 'Operator: Why did the queue grow?\nAgent: Only two fulfillment consumers were active; use four and a bounded ramp hold.\n'],
      ['prompt-history', 'prompt-edit-history.json', 'application/json', JSON.stringify({ versions: 2, edit: 'Added line-limit and ineligible-account and asynchronous fulfillment coverage.' }, null, 2)],
      ['tool-calls', 'tool-calls.json', 'application/json', JSON.stringify({ calls: ['jira_align.query', 'inventory.scan', 'apex_load.start', 'jira.create'] }, null, 2)],
      ['error-recovery', 'engine-recovery.md', 'text/markdown', '# Engine recovery\n\nThe native reservation was accepted; checkpoint reconciliation reused the idempotency key and avoided duplicate execution.'],
      ['release-decision', 'release-decision.json', 'application/json', JSON.stringify({ decision: 'conditional', blockers: ['PERF-248'], approvals: ['performance-lead'] }, null, 2)],
      ['results-archive', 'results.zip', 'application/octet-stream', 'PK\u0003\u0004offline-demo-results'],
    ]
    for (const [id, , mediaType, body] of rows) this.artifacts.set(`reports/${id}`, { mediaType, body })
    const refs = rows.map(([id, name, mediaType]) => artifactRef(id, name, id.includes('report') ? 'report' : 'evidence', mediaType, `Offline telecom demo artifact: ${name}`))
    for (const detail of this.pipelineDetails.values()) {
      const values = detail.values as PipelineState
      values.artifacts = refs
      detail.values = values as Record<string, unknown>
    }
  }

  private syncSummary(detail: PipelineDetail): PipelineDetail {
    const state = detail.values as PipelineState
    detail.phase_strip = PHASE_NAMES.map((phase) => ({
      phase,
      status: state.phases_plan && !state.phases_plan.includes(phase) ? 'none' : state.phase_results?.[phase]?.status ?? 'pending',
      attempt: state.phase_results?.[phase]?.attempt,
    }))
    const execution = state.phase_results?.execution
    const passed = execution?.status === 'succeeded' ? execution.test_summary?.passed : undefined
    detail.execution_result = passed === true ? 'passed' : passed === false ? 'failed' : null
    return detail
  }

  private toSummary(detail: PipelineDetail): PipelineSummary {
    const { thread_id, title, project_id, app_id, thread_status, current_phase, phase_strip, engine, created_at, updated_at, pending_gate, execution_result } = this.syncSummary(detail)
    return { thread_id, title, project_id, app_id, thread_status, current_phase, phase_strip, engine, created_at, updated_at, pending_gate, execution_result }
  }

  private agentAnalytics(params: URLSearchParams): AgentAnalytics {
    const rawGroupBy = params.get('group_by')
    const groupBy = AGENT_GROUP_BYS.includes(rawGroupBy as AgentGroupBy) ? (rawGroupBy as AgentGroupBy) : 'model'
    const bucket = params.get('bucket') === 'hour' ? 'hour' : 'day'
    const rawSort = params.get('sort')
    const sort = AGENT_SORTS.includes(rawSort as AgentSort) ? (rawSort as AgentSort) : 'total_tokens'
    const order = params.get('order') === 'asc' ? 'asc' : 'desc'
    const limit = Math.min(Math.max(parsePositiveInt(params.get('limit'), 20), 1), 100)
    const offset = parsePositiveInt(params.get('offset'), 0)
    const from = params.get('from') ?? isoMinutesAgo(bucket === 'hour' ? 8 * 60 : 7 * 1_440, this.activationTime)
    const to = params.get('to') ?? nowIso()
    const fromTime = new Date(from).getTime()
    const toTime = new Date(to).getTime()
    const models = new Set(parseMulti(params, 'model'))
    const stages = new Set(parseMulti(params, 'stage'))
    const agents = new Set(parseMulti(params, 'agent'))
    const project = params.get('project')
    const status = params.get('status') === 'ok' || params.get('status') === 'error' ? params.get('status') : null
    const test = params.get('test')?.toLowerCase() ?? null

    let events = this.agentMetricEvents(bucket).filter((event) => {
      const at = new Date(event.at).getTime()
      return (
        (Number.isNaN(fromTime) || at >= fromTime) &&
        (Number.isNaN(toTime) || at <= toTime) &&
        (!project || event.project_id === project) &&
        (models.size === 0 || models.has(event.model)) &&
        (stages.size === 0 || stages.has(event.phase)) &&
        (agents.size === 0 || agents.has(event.agent_name)) &&
        (!status || event.status === status) &&
        (!test || event.thread_id.toLowerCase().includes(test) || event.thread_title.toLowerCase().includes(test))
      )
    })

    const breakdownGroups = new Map<string, AgentMetricEvent[]>()
    for (const event of events) {
      const key = agentGroupKey(event, groupBy)
      breakdownGroups.set(key, [...(breakdownGroups.get(key) ?? []), event])
    }
    const breakdown = Array.from(breakdownGroups, ([key, grouped]) => agentBreakdownRow(key, grouped))
    breakdown.sort((left, right) => {
      const leftValue = left[sort as keyof AgentAnalyticsBreakdownRow]
      const rightValue = right[sort as keyof AgentAnalyticsBreakdownRow]
      let comparison = 0
      if (typeof leftValue === 'string' || typeof rightValue === 'string') {
        comparison = String(leftValue ?? '').localeCompare(String(rightValue ?? ''))
      } else {
        comparison = Number(leftValue ?? 0) - Number(rightValue ?? 0)
      }
      if (comparison === 0) comparison = left.key.localeCompare(right.key)
      return order === 'asc' ? comparison : -comparison
    })

    const seriesGroups = new Map<string, { bucketStart: string; key: string; events: AgentMetricEvent[] }>()
    for (const event of events) {
      const bucketStart = agentBucketStart(event.at, bucket)
      const key = agentGroupKey(event, groupBy)
      const groupId = `${bucketStart}\u0000${key}`
      const current = seriesGroups.get(groupId) ?? { bucketStart, key, events: [] }
      current.events.push(event)
      seriesGroups.set(groupId, current)
    }
    const series = Array.from(seriesGroups.values())
      .map((group) => agentSeriesPoint(group.bucketStart, group.key, group.events))
      .sort((left, right) => left.bucket_start.localeCompare(right.bucket_start) || left.key.localeCompare(right.key))

    const page = { limit, offset, total: breakdown.length }
    events = events.sort((left, right) => left.at.localeCompare(right.at))

    return {
      window: { from, to, bucket, group_by: groupBy },
      totals: agentTotals(events),
      breakdown: breakdown.slice(offset, offset + limit),
      series,
      page,
      cost_visible: true,
    }
  }

  private agentMetricEvents(bucket: 'day' | 'hour'): AgentMetricEvent[] {
    const phaseMetrics: Record<
      PhaseName,
      {
        model: string
        input: number
        output: number
        cacheRead: number
        cacheCreate: number
        reasoning: number
        latency: number
      }
    > = {
      story_analysis: {
        model: 'claude-sonnet-4-20250514',
        input: 11_500,
        output: 2_200,
        cacheRead: 2_600,
        cacheCreate: 700,
        reasoning: 150,
        latency: 4_200,
      },
      test_planning: {
        model: 'claude-sonnet-4-20250514',
        input: 7_600,
        output: 1_850,
        cacheRead: 1_200,
        cacheCreate: 420,
        reasoning: 120,
        latency: 5_200,
      },
      env_triage: {
        model: 'claude-3-5-haiku-20241022',
        input: 2_800,
        output: 650,
        cacheRead: 500,
        cacheCreate: 150,
        reasoning: 40,
        latency: 2_100,
      },
      script_scenario: {
        model: 'claude-sonnet-4-20250514',
        input: 13_200,
        output: 3_100,
        cacheRead: 1_800,
        cacheCreate: 650,
        reasoning: 100,
        latency: 6_100,
      },
      execution: {
        model: 'claude-3-5-haiku-20241022',
        input: 900,
        output: 280,
        cacheRead: 250,
        cacheCreate: 60,
        reasoning: 20,
        latency: 1_100,
      },
      reporting: {
        model: 'claude-opus-4-20250514',
        input: 15_800,
        output: 4_200,
        cacheRead: 2_300,
        cacheCreate: 900,
        reasoning: 250,
        latency: 7_600,
      },
      postmortem: {
        model: 'claude-sonnet-4-20250514',
        input: 6_400,
        output: 1_700,
        cacheRead: 800,
        cacheCreate: 250,
        reasoning: 130,
        latency: 4_800,
      },
    }
    const pricing: Record<string, { input: number; output: number; cacheRead: number; cacheCreate: number }> = {
      'claude-3-5-haiku-20241022': { input: 0.8, output: 4, cacheRead: 0.08, cacheCreate: 1 },
      'claude-sonnet-4-20250514': { input: 3, output: 15, cacheRead: 0.3, cacheCreate: 3.75 },
      'claude-opus-4-20250514': { input: 15, output: 75, cacheRead: 1.5, cacheCreate: 18.75 },
    }
    const details = Array.from(this.pipelineDetails.values()).sort((left, right) =>
      left.thread_id.localeCompare(right.thread_id),
    )
    const events: AgentMetricEvent[] = []
    details.forEach((detail, runIndex) => {
      PHASE_NAMES.forEach((phase, phaseIndex) => {
        const metric = phaseMetrics[phase]
        const runScale = detail.thread_id === 'run-aborted' ? 0.68 : 0.92 + runIndex * 0.07
        const phaseScale = 1 + phaseIndex * 0.018
        const scale = runScale * phaseScale
        const input = Math.round(metric.input * scale)
        const output = Math.round(metric.output * scale)
        const cacheRead = Math.round(metric.cacheRead * scale)
        const cacheCreate = Math.round(metric.cacheCreate * scale)
        const reasoning = Math.round(metric.reasoning * scale)
        const rates = pricing[metric.model]!
        const cost = Number(
          (
            (input * rates.input + output * rates.output + cacheRead * rates.cacheRead + cacheCreate * rates.cacheCreate) /
            1_000_000
          ).toFixed(6),
        )
        const minutesAgo =
          bucket === 'hour'
            ? (details.length - 1 - runIndex) * 55 + (PHASE_NAMES.length - phaseIndex) * 4
            : (details.length - 1 - runIndex) * 1_440 + (PHASE_NAMES.length - phaseIndex) * 75
        events.push({
          at: isoMinutesAgo(minutesAgo, this.activationTime),
          thread_id: detail.thread_id,
          thread_title: detail.title ?? detail.thread_id,
          project_id: detail.project_id ?? null,
          phase,
          agent_name: `${phase}.worker`,
          model: metric.model,
          provider: 'anthropic',
          status: detail.thread_id === 'run-aborted' && (phase === 'env_triage' || phase === 'execution') ? 'error' : 'ok',
          input_tokens: input,
          output_tokens: output,
          cache_read_tokens: cacheRead,
          cache_creation_tokens: cacheCreate,
          reasoning_tokens: reasoning,
          cost_usd: cost,
          latency_ms: Math.round(metric.latency * scale + runIndex * 85),
        })
      })
    })
    return events
  }

  private usage(params: URLSearchParams): UsageAnalytics {
    const bucket = (params.get('bucket') === 'hour' ? 'hour' : 'day') as 'day' | 'hour'
    const bucketCount = bucket === 'hour' ? 8 : 14
    const buckets = Array.from({ length: bucketCount }, (_, index) => ({
      bucket_start: bucket === 'hour' ? isoMinutesAgo((bucketCount - 1 - index) * 60, this.activationTime) : isoMinutesAgo((bucketCount - 1 - index) * 1_440, this.activationTime),
      events: 120 + index * 37,
      errors: index === 5 ? 22 : index + 2,
    }))
    return {
      window: { from: buckets[0]!.bucket_start, to: nowIso(), bucket },
      totals: {
        events: buckets.reduce((sum, item) => sum + item.events, 0),
        errors: buckets.reduce((sum, item) => sum + item.errors, 0),
        by_surface: { v1: 1_320, graph: 410 },
      },
      buckets,
      top_actions: [
        { action: 'pipelines.list', count: 420 },
        { action: 'logs.search', count: 220 },
        { action: 'work_tracking.query.execute', count: 120 },
        { action: 'gates.resume', count: 18 },
      ],
      runs: { phases_succeeded: 88, phases_failed: 7 },
    }
  }

  private async searchLogs(request: Request): Promise<components['schemas']['LogSearchResponse']> {
    const body = (await request.json()) as LogSearchRequest
    const text = body.query?.text?.toLowerCase()
    const filters = body.query?.filters ?? {}
    let rows = this.logs.filter((entry) => matchesText([entry.message, entry.service, entry.level], text ?? null))
    const level = filters['level']
    const service = filters['service']
    const threadId = filters['thread_id']
    if (level) rows = rows.filter((entry) => entry.level.toLowerCase() === level.toLowerCase())
    if (service) rows = rows.filter((entry) => entry.service === service)
    if (threadId) rows = rows.filter((entry) => String(entry.fields?.['thread_id'] ?? '') === threadId)
    const page = { limit: body.limit, offset: body.offset, items: rows.slice(body.offset, body.offset + body.limit) }
    return { entries: page.items, total: rows.length, limit: page.limit, offset: page.offset, window: { from: body.window?.from ?? isoMinutesAgo(60, this.activationTime), to: body.window?.to ?? this.activationTime } }
  }

  private workTrackingBinding(
    connectionId: string | null | undefined,
    providerHint?: string,
  ): WorkTrackingBinding {
    const byConnection: Record<string, string> = {
      'conn-align': 'jira_align',
      'conn-jira': 'jira',
    }
    if (connectionId) {
      return { connection_id: connectionId, provider: byConnection[connectionId] ?? 'jira' }
    }
    const normalizedProvider = providerHint?.toLowerCase()
    for (const [connection, provider] of Object.entries(byConnection)) {
      if (normalizedProvider === provider) return { connection_id: connection, provider }
    }
    return { connection_id: 'conn-jira', provider: 'jira' }
  }

  private workItemPage(
    rows: WorkItem[],
    limit: number,
    offset: number,
    binding: WorkTrackingBinding,
  ): WorkItemPage {
    return {
      ...binding,
      items: rows.slice(offset, offset + limit),
      total: rows.length,
      page: { limit, offset },
    }
  }

  private persistState(): void {
    if (typeof localStorage === 'undefined') return
    try {
      const emptyMap = new Map<string, never>()
      localStorage.setItem(
        DEMO_STATE_STORAGE_KEY,
        JSON.stringify({
          version: DEMO_STATE_VERSION,
          activationTime: this.activationTime,
          nextId: this.nextId,
          mutations: {
            applications: arrayDelta(this.applications, this.baseline.applications, (item) => item.id),
            environments: arrayDelta(this.environments, this.baseline.environments, (item) => item.id),
            inventories: mapDelta(this.inventories, this.baseline.inventories),
            connections: arrayDelta(this.connections, this.baseline.connections, (item) => item.id),
            hostMappings: mapDelta(this.hostMappings, this.baseline.hostMappings),
            consumers: arrayDelta(this.consumers, this.baseline.consumers, (item) => item.id),
            documents: arrayDelta(this.documents, this.baseline.documents, (item) => item.id),
            drafts: arrayDelta(this.drafts, this.baseline.drafts, (item) => item.id),
            workItems: arrayDelta(this.workItems, this.baseline.workItems, (item) => item.key),
            workItemProviders: mapDelta(this.workItemProviders, this.baseline.workItemProviders),
            savedQueries: arrayDelta(this.savedQueries, this.baseline.savedQueries, (item) => item.id),
            prompts: arrayDelta(this.prompts, this.baseline.prompts, (item) => item.id),
            pipelineDetails: mapDelta(this.pipelineDetails, this.baseline.pipelineDetails),
            runs: mapDelta(this.runs, this.baseline.runs),
            assistants: arrayDelta(this.assistants, this.baseline.assistants, (item) => item.assistant_id),
            artifacts: mapDelta(this.artifacts, this.baseline.artifacts),
            runStages: mapDelta(this.runStages, this.baseline.runStages),
            runEvents: mapDelta(this.runEvents, emptyMap),
            contextSummaryJobs: mapDelta(this.contextSummaryJobs, emptyMap),
            apexIdempotency: mapDelta(this.apexIdempotency, emptyMap),
            reportingTickets: mapDelta(this.reportingTickets, emptyMap),
            pipelineLaunches: mapDelta(this.pipelineLaunches, emptyMap),
            apexScenarios: arrayDelta(this.apexScenarios, this.baseline.apexScenarios, (item) => String(item['id'])),
            apexSchedules: arrayDelta(this.apexSchedules, this.baseline.apexSchedules, (item) => String(item['id'])),
            apexOccurrences: arrayDelta(this.apexOccurrences, this.baseline.apexOccurrences, (item) => String(item['id'])),
          },
          completedRunStreams: [...this.completedRunStreams],
        }),
      )
    } catch {
      // Storage can be unavailable in private browsing; the in-memory demo remains usable.
    }
  }

  private restorePersistedState(): void {
    if (typeof localStorage === 'undefined') return
    const raw = localStorage.getItem(DEMO_STATE_STORAGE_KEY)
    if (!raw) return
    try {
      const state = JSON.parse(raw) as Record<string, unknown>
      if (state['version'] !== DEMO_STATE_VERSION) throw new Error('incompatible demo journal')
      if (typeof state['activationTime'] !== 'string' || Number.isNaN(Date.parse(state['activationTime']))) throw new Error('corrupt demo activation time')
      if (!Number.isSafeInteger(state['nextId']) || Number(state['nextId']) < 1) throw new Error('corrupt demo sequence')
      const mutations = state['mutations']
      if (!mutations || typeof mutations !== 'object' || Array.isArray(mutations)) throw new Error('corrupt demo mutations')
      const values = mutations as Record<string, unknown>
      const readArray = <T>(name: string, keyOf: (item: T) => string): ArrayDelta<T> | undefined => {
        const value = values[name]
        if (value === undefined) return undefined
        if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`corrupt ${name} delta`)
        const delta = value as { upsert?: unknown; removed?: unknown }
        if (!Array.isArray(delta.upsert) || !Array.isArray(delta.removed) || !delta.removed.every((key) => typeof key === 'string')) throw new Error(`corrupt ${name} delta`)
        for (const item of delta.upsert as T[]) if (!item || typeof item !== 'object' || !keyOf(item)) throw new Error(`corrupt ${name} item`)
        return delta as ArrayDelta<T>
      }
      const readMap = <T>(name: string): MapDelta<T> | undefined => {
        const value = values[name]
        if (value === undefined) return undefined
        if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`corrupt ${name} delta`)
        const delta = value as { upsert?: unknown; removed?: unknown }
        if (!Array.isArray(delta.upsert) || !Array.isArray(delta.removed) || !delta.removed.every((key) => typeof key === 'string')) throw new Error(`corrupt ${name} delta`)
        if (!delta.upsert.every((entry) => Array.isArray(entry) && entry.length === 2 && typeof entry[0] === 'string')) throw new Error(`corrupt ${name} entries`)
        return delta as MapDelta<T>
      }
      const completed = state['completedRunStreams']
      if (!Array.isArray(completed) || !completed.every((id) => typeof id === 'string')) throw new Error('corrupt completed stream cursors')

      this.activationTime = state['activationTime']
      this.nextId = Number(state['nextId'])
      this.applications = applyArrayDelta(this.baseline.applications, readArray<Application>('applications', (item) => item.id), (item) => item.id)
      this.environments = applyArrayDelta(this.baseline.environments, readArray<Environment>('environments', (item) => item.id), (item) => item.id)
      this.inventories = applyMapDelta(this.baseline.inventories, readMap<InventoryView>('inventories'))
      this.connections = applyArrayDelta(this.baseline.connections, readArray<Connection>('connections', (item) => item.id), (item) => item.id)
      this.hostMappings = applyMapDelta(this.baseline.hostMappings, readMap<HostMappingOut[]>('hostMappings'))
      this.consumers = applyArrayDelta(this.baseline.consumers, readArray<Consumer>('consumers', (item) => item.id), (item) => item.id)
      this.documents = applyArrayDelta(this.baseline.documents, readArray<DocumentOut>('documents', (item) => item.id), (item) => item.id)
      this.drafts = applyArrayDelta(this.baseline.drafts, readArray<DraftRead>('drafts', (item) => item.id), (item) => item.id)
      this.workItems = applyArrayDelta(this.baseline.workItems, readArray<WorkItem>('workItems', (item) => item.key), (item) => item.key)
      this.workItemProviders = applyMapDelta(this.baseline.workItemProviders, readMap<'jira' | 'jira_align'>('workItemProviders'))
      this.savedQueries = applyArrayDelta(this.baseline.savedQueries, readArray<SavedQuery>('savedQueries', (item) => item.id), (item) => item.id)
      this.prompts = applyArrayDelta(this.baseline.prompts, readArray<PromptRecord>('prompts', (item) => item.id), (item) => item.id)
      this.pipelineDetails = applyMapDelta(this.baseline.pipelineDetails, readMap<PipelineDetail>('pipelineDetails'))
      this.runs = applyMapDelta(this.baseline.runs, readMap<Run[]>('runs'))
      this.assistants = applyArrayDelta(this.baseline.assistants, readArray<Assistant>('assistants', (item) => item.assistant_id), (item) => item.assistant_id)
      this.artifacts = applyMapDelta(this.baseline.artifacts, readMap<StoredArtifact>('artifacts'))
      this.runStages = applyMapDelta(this.baseline.runStages, readMap<string>('runStages'))
      this.runEvents = applyMapDelta(new Map(), readMap<DemoStreamEvent[]>('runEvents'))
      this.contextSummaryJobs = applyMapDelta(new Map(), readMap<ContextSummaryJob>('contextSummaryJobs'))
      this.apexIdempotency = applyMapDelta(new Map(), readMap<{ status: number; body: Record<string, unknown> }>('apexIdempotency'))
      this.reportingTickets = applyMapDelta(new Map(), readMap<string>('reportingTickets'))
      this.pipelineLaunches = applyMapDelta(new Map(), readMap<StartPipelineResponse>('pipelineLaunches'))
      this.apexScenarios = applyArrayDelta(this.baseline.apexScenarios, readArray<Record<string, unknown>>('apexScenarios', (item) => String(item['id'] ?? '')), (item) => String(item['id']))
      this.apexSchedules = applyArrayDelta(this.baseline.apexSchedules, readArray<Record<string, unknown>>('apexSchedules', (item) => String(item['id'] ?? '')), (item) => String(item['id']))
      this.apexOccurrences = applyArrayDelta(this.baseline.apexOccurrences, readArray<Record<string, unknown>>('apexOccurrences', (item) => String(item['id'] ?? '')), (item) => String(item['id']))
      this.completedRunStreams = new Set(completed)
    } catch {
      this.restoreBaseline()
      localStorage.removeItem(DEMO_STATE_STORAGE_KEY)
    }
  }

  private id(prefix: string): string {
    return `${prefix}-${this.nextId++}`
  }

  private fingerprint(): string {
    return Math.random().toString(16).slice(2, 10).padEnd(8, '0')
  }
}

export function createDevDataStore(): DevDataStore {
  return new DevDataStore()
}
