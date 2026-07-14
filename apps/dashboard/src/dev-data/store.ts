import type { components } from '@apex/api-client'
import type { Assistant, Run, Thread } from '@langchain/langgraph-sdk'

import { PHASE_NAMES, type PhaseName, type PipelineState } from '@apex/pipeline-events'

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
type TranslatedQuery = components['schemas']['TranslatedQuery']
type AgentAnalytics = components['schemas']['AgentAnalyticsResponse']
type AgentAnalyticsBreakdownRow = components['schemas']['AgentAnalyticsBreakdownRow']
type AgentAnalyticsSeriesPoint = components['schemas']['AgentAnalyticsSeriesPoint']
type AgentAnalyticsTotals = components['schemas']['AgentAnalyticsTotals']
type AgentGroupBy = AgentAnalytics['window']['group_by']
type UsageAnalytics = components['schemas']['UsageAnalyticsResponse']
type WorkItem = components['schemas']['WorkItem']
type WorkItemDraft = components['schemas']['WorkItemDraft']
type WorkItemPage = components['schemas']['WorkItemPage']

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

interface PromptRecord extends PromptDetail {
  versions: PromptVersionDetail[]
}

const JSON_HEADERS = { 'content-type': 'application/json' }
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

function isoMinutesAgo(minutes: number): string {
  return new Date(Date.now() - minutes * 60_000).toISOString()
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
) {
  return {
    id,
    name,
    kind,
    media_type: mediaType,
    uri: `memory://reports/${id}`,
    summary: summary ?? null,
    created_at: isoMinutesAgo(35),
  }
}

function makePipelineState(
  title: string,
  currentPhase: PhaseName,
  status: 'busy' | 'interrupted' | 'idle' | 'error',
): PipelineState {
  const report = artifactRef('exec-report', 'load-report.json', 'report', 'application/json', 'Normalized KPI summary')
  const transcript = artifactRef(
    'execution-transcript',
    'execution transcript.txt',
    'transcript',
    'text/plain',
    'Execution agent transcript',
  )
  const archive = artifactRef('results-archive', 'results.zip', 'archive', 'application/octet-stream', 'Raw engine results')
  const promptReviews = Object.fromEntries(
    PHASE_NAMES.map((phase) => [
      phase,
      {
        system: `You are the ${phase.replaceAll('_', ' ')} phase operator.`,
        phase_prompt: `Prepare the ${phase.replaceAll('_', ' ')} deliverable for: ${title}.`,
        application: 'Checkout-specific requirements: preserve carts through payment retries and report p95 latency.',
        additional_context: '',
        source: { origin: 'catalog', ref: `phase/${phase}@dev` },
        updated_at: isoMinutesAgo(140),
        updated_by: 'system',
      },
    ]),
  ) as PipelineState['prompt_reviews']
  return {
    title,
    request: 'Validate checkout and search latency before the release train.',
    current_phase: currentPhase,
    phases_plan: [...PHASE_NAMES],
    prompt_reviews: promptReviews,
    phase_results: {
      story_analysis: {
        phase: 'story_analysis',
        status: 'succeeded',
        attempt: 1,
        started_at: isoMinutesAgo(140),
        ended_at: isoMinutesAgo(138),
        duration_s: 105,
        summary: 'Scoped checkout, search, and account-auth journeys from the linked stories.',
        reasoning_digest: 'Focused on traffic paths with recent defect churn.',
        resolved_prompt: {
          system: 'You are the story analysis phase operator.',
          user: 'Analyze PHX-101 and PHX-102 for load-test scope.',
        },
        resolved_prompt_source: { origin: 'catalog', ref: 'phase/story_analysis/system@v3' },
      },
      test_planning: {
        phase: 'test_planning',
        status: status === 'interrupted' && currentPhase === 'test_planning' ? 'awaiting_prompt_review' : 'succeeded',
        attempt: 1,
        started_at: isoMinutesAgo(132),
        ended_at: isoMinutesAgo(128),
        duration_s: 220,
        summary: 'Planned a 25 minute ramp/soak with checkout, search, and auth traffic mix.',
        warnings: ['Checkout ramp is close to the configured p95 budget.'],
      },
      env_triage: {
        phase: 'env_triage',
        status: 'succeeded',
        attempt: 1,
        started_at: isoMinutesAgo(125),
        ended_at: isoMinutesAgo(123),
        duration_s: 94,
        summary: 'Staging inventory is available; one search replica is under-provisioned.',
      },
      script_scenario: {
        phase: 'script_scenario',
        status: 'succeeded',
        attempt: 1,
        started_at: isoMinutesAgo(118),
        ended_at: isoMinutesAgo(112),
        duration_s: 360,
        summary: 'Generated checkout and search scripts with explicit SLA assertions.',
        load_test_spec: {
          title,
          target_environment: 'env-staging',
          vusers: 450,
          ramp_s: 300,
          duration_s: 1500,
          slas: { p95_ms: 450, error_rate: 0.01 },
          script_refs: ['memory://scripts/checkout.js', 'memory://scripts/search.js'],
        },
      },
      execution: {
        phase: 'execution',
        status: status === 'busy' ? 'running' : status === 'error' ? 'failed' : 'succeeded',
        attempt: status === 'error' ? 2 : 1,
        started_at: isoMinutesAgo(90),
        ended_at: status === 'busy' ? null : isoMinutesAgo(58),
        duration_s: status === 'busy' ? null : 1920,
        summary:
          status === 'error'
            ? 'Execution failed after the engine reported sustained gateway 502s.'
            : 'Load test stayed inside p95 budget with a small payment retry spike.',
        warnings: status === 'error' ? ['Gateway error rate exceeded 4% for three minutes.'] : [],
        artifact_ids: [report.id, archive.id, transcript.id],
        engine: 'apexload',
        engine_started_at: isoMinutesAgo(89),
        engine_handle: { engine: 'apexload', connection_id: 'conn-engine', external_run_id: 'al-dev-42' },
        test_summary: {
          engine: 'apexload',
          passed: status !== 'error',
          kpis: { tps_avg: 148.2, p95_ms: status === 'error' ? 880 : 312, error_rate: status === 'error' ? 0.043 : 0.006, vusers_peak: 450 },
          sla_breaches: status === 'error' ? ['p95_ms', 'error_rate'] : [],
          notes: status === 'error' ? 'Payment gateway retries saturated.' : 'Within configured release gate.',
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
        started_at: status === 'busy' ? null : isoMinutesAgo(50),
        ended_at: status === 'busy' ? null : isoMinutesAgo(45),
        duration_s: status === 'busy' ? null : 290,
        summary: 'Drafted release summary and KPI comparison.',
      },
      postmortem: {
        phase: 'postmortem',
        status: status === 'error' ? 'running' : 'pending',
        attempt: status === 'error' ? 1 : undefined,
        started_at: status === 'error' ? isoMinutesAgo(30) : null,
      },
    },
    artifacts: [report, archive, transcript],
    dialogue: [
      {
        id: 'dlg-1',
        phase: 'test_planning',
        role: 'operator',
        content: 'Keep checkout at 70% of the mix and include payment retries.',
        at: isoMinutesAgo(127),
      },
      {
        id: 'dlg-2',
        phase: 'test_planning',
        role: 'agent',
        content: 'Traffic mix adjusted and retry assertions added to the plan.',
        at: isoMinutesAgo(126),
      },
    ],
    context_packets: [
      {
        id: 'ctx-1',
        source: 'jira',
        title: 'PHX-101 acceptance criteria',
        summary: 'Checkout must absorb gateway retries without dropping carts.',
        ref: 'jira:PHX-101',
      },
    ],
    engine_handle: { engine: 'apexload', connection_id: 'conn-engine', external_run_id: 'al-dev-42' },
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
  private savedQueries: SavedQuery[]
  private prompts: PromptRecord[]
  private logs: LogEntry[]
  private pipelineDetails = new Map<string, PipelineDetail>()
  private runs = new Map<string, Run[]>()
  private assistants: Assistant[]
  private artifacts = new Map<string, StoredArtifact>()

  readonly systemInfo: SystemInfo = {
    name: 'APEX Orchestration Engine',
    version: 'dev-dummy',
    environment: 'development',
    features: { engines: true, documents: true, dummy_data: true },
    consumer: {
      name: 'Dev Admin',
      role: 'admin',
      scopes: [{ project_id: 'proj-alpha', app_id: null }],
    },
  }

  constructor() {
    const created = isoMinutesAgo(8_000)
    const updated = isoMinutesAgo(20)
    this.applications = [
      { id: 'app-checkout', name: 'Checkout', project_id: 'proj-alpha', description: 'Payment funnel services', archived_at: null, created_at: created, updated_at: updated },
      { id: 'app-search', name: 'Search', project_id: 'proj-alpha', description: 'Search and product discovery', archived_at: null, created_at: created, updated_at: updated },
      { id: 'app-billing', name: 'Billing', project_id: 'proj-beta', description: 'Secondary project with sparse activity', archived_at: null, created_at: created, updated_at: updated },
    ]
    this.environments = [
      this.environment('env-staging', 'app-checkout', 'staging', 'k8s', 'https://staging.checkout.example.com', false),
      this.environment('env-prod', 'app-checkout', 'production', 'k8s', 'https://checkout.example.com', true),
      this.environment('env-search-dev', 'app-search', 'dev', 'vm', null, false),
    ]
    this.seedInventories()
    this.connections = [
      this.connection('conn-jira', 'work_tracking', 'jira', 'jira-prod', 'proj-alpha', 'https://jira.example.com', true, { project_key: 'PHX' }, 'env:JIRA_TOKEN'),
      this.connection('conn-ado', 'work_tracking', 'azure_devops', 'ado-beta', 'proj-beta', 'https://dev.azure.com/example', true, { project: 'Billing' }, 'env:ADO_TOKEN'),
      this.connection('conn-elk', 'log_search', 'elasticsearch', 'elk-global', null, 'https://elk.example.com:9200', false, {}, null),
      this.connection('conn-engine', 'execution_engine', 'apex_load', 'apex-load-default', 'proj-alpha', null, true, { pool: 'dev' }, null),
      this.connection('conn-docs', 'documents', 'stub', 'document-store', null, null, true, {}, null),
      this.connection('conn-inventory', 'cluster_inventory', 'k8s', 'staging-cluster', 'proj-alpha', null, true, { kube_context: 'staging' }, 'env:KUBECONFIG'),
    ]
    this.hostMappings.set('conn-jira', [
      { id: 'map-1', pattern: '*.internal.example.com', target: 'proxy.example.com', enabled: true },
      { id: 'map-2', pattern: 'jira.local', target: 'jira.example.com', enabled: false },
    ])
    this.consumers = [
      this.consumer('cons-admin', 'Dev Admin', 'admin', 'dashboard', true, 'ad41de01'),
      this.consumer('cons-operator', 'Load Operator', 'operator', 'dashboard', true, '0a0b0c0d'),
      this.consumer('cons-viewer', 'Read Only Reviewer', 'viewer', 'dashboard', true, '1111aaaa'),
      this.consumer('cons-ci', 'CI Smoke Runner', 'operator', 'headless', false, 'c1c2c3c4'),
    ]
    this.documents = [
      this.document('doc-spec', 'checkout-spec.pdf', 'application/pdf', 2_097_152, 'Perf test requirements and acceptance criteria.'),
      this.document('doc-runbook', 'perf-runbook.md', 'text/markdown', 11_264, 'Load-test response runbook.'),
      this.document('doc-incident', 'gateway-incident.json', 'application/json', 4_512, 'Recent gateway incident export.'),
    ]
    this.drafts = [
      this.draft('draft-checkout', 'Checkout regression run', 'proj-alpha', {
        title: 'Checkout regression run',
        request: 'Run checkout and search regression with payment retries.',
        project_id: 'proj-alpha',
      }),
      this.draft('draft-beta', 'Billing smoke run', 'proj-beta', {
        title: 'Billing smoke run',
        request: 'Short billing smoke against beta.',
        project_id: 'proj-beta',
      }),
    ]
    this.evidence = [
      { id: 'ev-jira-1', source: 'jira', title: 'PHX-101 acceptance criteria', summary: 'Checkout must absorb gateway retries without dropping carts.', ref: 'jira:PHX-101', thread_id: 'run-gated-prompt' },
      { id: 'ev-elk-1', source: 'elk', title: '502 spike during ramp', summary: 'Gateway errors crossed 2% at 400 vusers.', ref: 'elk:gateway-502', thread_id: 'run-failed' },
      { id: 'ev-doc-1', source: 'documents', title: 'Perf runbook', summary: 'Escalation and rollback checklist for load tests.', ref: 'doc-runbook', thread_id: null },
    ]
    this.workItems = [
      { key: 'PHX-101', title: 'Checkout retries drop payments', kind: 'story', status: 'open', description: 'Retries on the payment gateway drop the cart.', url: 'https://tracker.example.com/browse/PHX-101' },
      { key: 'PHX-102', title: 'Gateway 502s under load', kind: 'bug', status: 'in_progress', description: 'Gateway fails during peak ramp.', url: null },
      { key: 'PHX-130', title: 'Search typeahead p95 budget', kind: 'story', status: 'ready_for_test', description: 'Search should stay below 280ms p95.', url: 'https://tracker.example.com/browse/PHX-130' },
      { key: 'BILL-44', title: 'Billing export smoke', kind: 'task', status: 'open', description: 'Exercise secondary project scoping.', url: null },
    ]
    this.savedQueries = [
      { id: 'sq-open', name: 'Open payment stories', provider: 'jira', query: 'project = PHX AND status in (Open, "In Progress")', description: 'Sprint triage pick list', project_id: 'proj-alpha', created_by: 'ops', created_at: created, updated_at: updated },
      { id: 'sq-bugs', name: 'Load bugs', provider: 'jira', query: 'project = PHX AND labels = load-test AND type = Bug', description: null, project_id: 'proj-alpha', created_by: 'ops', created_at: created, updated_at: updated },
    ]
    this.prompts = this.makePrompts(created, updated)
    this.logs = this.makeLogs()
    this.assistants = this.makeAssistants(created, updated)
    this.seedPipelines()
    this.seedArtifacts()
  }

  async handleApexRequest(request: Request): Promise<Response | null> {
    const url = new URL(request.url)
    const path = url.pathname.replace(/\/+$/, '') || '/'
    const method = request.method.toUpperCase()
    if (!path.startsWith('/v1/')) return null

    if (method === 'GET' && path === '/v1/system/info') return jsonResponse(this.systemInfo)
    if (method === 'GET' && path === '/v1/analytics/agents') return jsonResponse(this.agentAnalytics(url.searchParams))
    if (method === 'GET' && path === '/v1/analytics/usage') return jsonResponse(this.usage(url.searchParams))
    if (method === 'POST' && path === '/v1/logs/search') return jsonResponse(await this.searchLogs(request))
    if (path.startsWith('/v1/pipelines')) return this.handlePipelines(method, path, request, url.searchParams)
    if (path.startsWith('/v1/catalog/applications')) return this.handleApplications(method, path, request, url.searchParams)
    if (path.startsWith('/v1/catalog/environments')) return this.handleEnvironments(method, path, request, url.searchParams)
    if (path.startsWith('/v1/inventory/environments')) return this.handleInventory(method, path)
    if (path.startsWith('/v1/work-tracking')) return this.handleWorkTracking(method, path, request, url.searchParams)
    if (path.startsWith('/v1/context')) return this.handleContext(method, path, request, url.searchParams)
    if (path.startsWith('/v1/documents')) return this.handleDocuments(method, path, request, url.searchParams)
    if (path.startsWith('/v1/drafts')) return this.handleDrafts(method, path, request, url.searchParams)
    if (path.startsWith('/v1/prompts')) return this.handlePrompts(method, path, request, url.searchParams)
    if (path.startsWith('/v1/admin/connections')) return this.handleConnections(method, path, request, url.searchParams)
    if (path.startsWith('/v1/admin/consumers')) return this.handleConsumers(method, path, request)

    return problemResponse(501, 'dummy_handler_missing', `Dummy data has no handler for ${method} ${path}.`)
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

  searchAssistants(): Assistant[] {
    return clone(this.assistants)
  }

  getAssistant(assistantId: string): Assistant {
    const assistant = this.assistants.find((item) => item.assistant_id === assistantId)
    if (!assistant) throw new Error(`Dummy assistant ${assistantId} not found`)
    return clone(assistant)
  }

  updateAssistant(assistantId: string, payload: { config?: { configurable?: Record<string, unknown> } }): Assistant {
    const index = this.assistants.findIndex((item) => item.assistant_id === assistantId)
    if (index === -1) throw new Error(`Dummy assistant ${assistantId} not found`)
    const current = this.assistants[index]!
    const updated: Assistant = {
      ...current,
      config: { ...(current.config ?? {}), configurable: payload.config?.configurable ?? {} },
      version: current.version + 1,
      updated_at: nowIso(),
    }
    this.assistants[index] = updated
    return clone(updated)
  }

  createThread(payload?: { metadata?: Record<string, unknown> }): Thread<Record<string, unknown>> {
    const id = this.id('thread')
    const created = nowIso()
    const detail = this.pipelineDetail(id, 'Untitled dummy run', payload?.metadata?.['project_id'] as string | undefined, 'busy', 'story_analysis')
    this.pipelineDetails.set(id, detail)
    this.runs.set(id, [])
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
    const title = typeof input['title'] === 'string' ? input['title'] : this.pipelineDetails.get(tid)?.title || 'Dummy rerun'
    const firstPhase = Array.isArray(payload?.config?.configurable?.['phases'])
      ? (payload?.config?.configurable?.['phases'] as string[])[0]
      : 'story_analysis'
    const phase = PHASE_NAMES.includes(firstPhase as PhaseName) ? (firstPhase as PhaseName) : 'story_analysis'
    const detail = this.pipelineDetails.get(tid) ?? this.pipelineDetail(tid, title, 'proj-alpha', 'busy', phase)
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
    return clone(run)
  }

  listRuns(threadId: string): Run[] {
    return clone(this.runs.get(threadId) ?? [])
  }

  async *joinRunStream(
    threadId: string | undefined | null,
    runId: string,
    options?: { signal?: AbortSignal },
  ): AsyncGenerator<{ id?: string; event: string; data: unknown }> {
    const tid = threadId ?? 'thread'
    const samples = [
      { progress_pct: 0.12, vusers: 80, tps: 42, error_rate: 0.001, p95_ms: 240, status: 'provisioning' },
      { progress_pct: 0.38, vusers: 220, tps: 96, error_rate: 0.004, p95_ms: 288, status: 'running' },
      { progress_pct: 0.72, vusers: 450, tps: 151, error_rate: 0.006, p95_ms: 318, status: 'running' },
    ] as const
    yield {
      id: `${runId}-plan`,
      event: 'custom',
      data: { schema_version: 1, type: 'plan_resolved', phases: PHASE_NAMES },
    }
    yield {
      id: `${runId}-tool`,
      event: 'custom',
      data: { schema_version: 1, type: 'tool_call', phase: 'execution', id: 'tool-dev-1', tool: 'apex_load.start', status: 'ok' },
    }
    for (const [index, sample] of samples.entries()) {
      if (options?.signal?.aborted) return
      yield {
        id: `${runId}-engine-${index}`,
        event: 'custom',
        data: {
          schema_version: 1,
          type: 'engine_poll',
          phase: 'execution',
          attempt: 1,
          engine: 'apexload',
          external_run_id: `dummy-${tid}`,
          status: sample.status,
          progress_pct: sample.progress_pct,
          live_stats: {
            vusers: sample.vusers,
            tps: sample.tps,
            error_rate: sample.error_rate,
            p95_ms: sample.p95_ms,
          },
        },
      }
      await new Promise((resolve) => setTimeout(resolve, 25))
    }
  }

  private handlePipelines(method: string, path: string, request: Request, params: URLSearchParams): Promise<Response> | Response {
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
        additional_context: '',
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
      return detail ? jsonResponse(detail) : problemResponse(404, 'not_found', `Run ${threadId} was not found.`)
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
        if (detail) {
          const action = (body as { action?: string }).action ?? 'approve'
          detail.pending_gate = null
          detail.interrupts = []
          detail.thread_status = action === 'abort' ? 'error' : 'busy'
          detail.current_phase = action === 'abort' ? detail.current_phase : 'execution'
          detail.updated_at = nowIso()
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
        project_id: body.project_id ?? 'proj-alpha',
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
      const env = this.environment(this.id('env'), body.application_id ?? 'app-checkout', body.name ?? 'new-env', body.kind ?? 'k8s', body.base_url ?? null, false)
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
            { name: 'checkout-api', image: 'ghcr.io/apex/checkout:1.47.0', replicas: 4 },
            { name: 'payments-gateway', image: 'ghcr.io/apex/payments:2.11.3', replicas: 3 },
            { name: 'search-indexer', image: 'ghcr.io/apex/search:0.93.1', replicas: 2 },
          ],
        },
      }
      this.inventories.set(environmentId, fresh)
      return jsonResponse(fresh)
    }
    return problemResponse(501, 'dummy_handler_missing', `Dummy data has no handler for ${method} ${path}.`)
  }

  private async handleWorkTracking(method: string, path: string, request: Request, params: URLSearchParams): Promise<Response> {
    if (method === 'POST' && path === '/v1/work-tracking/query/translate') {
      const body = (await request.json()) as { text?: string }
      const text = body.text ?? 'open load test bugs'
      const provider = text.toLowerCase().includes('azure') ? 'ado' : 'jira'
      return jsonResponse({ provider, query: provider === 'jira' ? `project = PHX AND text ~ "${text}"` : `SELECT [System.Id] FROM WorkItems WHERE [System.Title] CONTAINS '${text}'`, confidence: 0.86 } satisfies TranslatedQuery)
    }
    if (method === 'POST' && path === '/v1/work-tracking/query/execute') {
      const body = (await request.json()) as { limit?: number; offset?: number; query?: TranslatedQuery }
      return jsonResponse(this.workItemPage(this.workItems, body.limit ?? 25, body.offset ?? 0))
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
      const saved: SavedQuery = { id: this.id('sq'), created_at: nowIso(), updated_at: nowIso(), created_by: 'dev', description: body.description ?? null, name: body.name, provider: body.provider, query: body.query, project_id: body.project_id ?? null }
      this.savedQueries.unshift(saved)
      return jsonResponse(saved, 201)
    }
    const savedMatch = /^\/v1\/work-tracking\/saved-queries\/([^/]+)$/.exec(path)
    if (savedMatch) return this.handleSavedQuery(method, decodePart(savedMatch[1]), request)
    if (method === 'POST' && path === '/v1/work-tracking/items') {
      const body = (await request.json()) as WorkItemDraft
      const key = `PHX-${200 + this.nextId++}`
      const item: WorkItem = { key, title: body.title, description: body.description ?? '', kind: body.kind ?? 'story', status: 'open', url: null }
      this.workItems.unshift(item)
      return jsonResponse(item, 201)
    }
    const itemMatch = /^\/v1\/work-tracking\/items\/([^/]+)(?:\/enrich)?$/.exec(path)
    if (itemMatch) {
      const key = decodePart(itemMatch[1])
      const item = this.workItems.find((row) => row.key === key)
      if (!item) return problemResponse(404, 'not_found', `Work item ${key} was not found.`)
      if (method === 'GET') return jsonResponse(item)
      if (method === 'POST' && path.endsWith('/enrich')) {
        const body = (await request.json()) as { comment?: string | null }
        item.description = [item.description, body.comment].filter(Boolean).join('\n\n')
        return jsonResponse(item)
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

  private async handleContext(method: string, path: string, request: Request, params: URLSearchParams): Promise<Response> {
    if (method === 'GET' && path === '/v1/context/evidence') {
      const project = params.get('project')
      const threadId = params.get('thread_id')
      return jsonResponse(this.evidence.filter((item) => (!threadId || item.thread_id === threadId) && (!project || true)))
    }
    if (method === 'POST' && path === '/v1/context/summaries') {
      const body = (await request.json()) as ContextSummaryRequest
      const threadId = this.id('ctx-thread')
      return jsonResponse({ run_id: this.id('ctx-run'), stream_url: `/threads/${threadId}/runs/${this.id('run')}/stream`, subject: body.subject }, 202)
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
      const doc = this.document(this.id('doc'), name, file instanceof File ? file.type || 'application/octet-stream' : 'text/plain', size, String(form.get('summary') ?? 'Uploaded in dummy mode.'))
      doc.project_id = String(form.get('project_id') ?? 'proj-alpha')
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
    if (method === 'POST' && parts[4] === 'test') return jsonResponse({ run_id: this.id('prompt-run'), thread_id: this.id('prompt-thread') }, 202)
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
      const probe: ProbeResult = { ok: conn.enabled, detail: conn.enabled ? 'Dummy probe succeeded.' : 'Connection is disabled in dummy data.', latency_ms: conn.enabled ? 42 : 0 }
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
      options: { namespace: `${applicationId}-${name}` },
      created_at: isoMinutesAgo(12_000),
      updated_at: isoMinutesAgo(30),
      last_snapshot: { scanned_at: isoMinutesAgo(stale ? 2_400 : 15), service_count: stale ? 2 : 4 },
    }
  }

  private seedInventories(): void {
    for (const env of this.environments) {
      this.inventories.set(env.id, {
        environment_id: env.id,
        snapshot: {
          scanned_at: env.last_snapshot?.scanned_at ?? isoMinutesAgo(15),
          stale: env.id === 'env-prod',
          services: [
            { name: 'checkout-api', image: 'ghcr.io/apex/checkout:1.47.0', replicas: env.id === 'env-prod' ? 8 : 3 },
            { name: 'payments-gateway', image: 'ghcr.io/apex/payments:2.11.3', replicas: 3 },
            { name: 'search-api', image: 'ghcr.io/apex/search:0.93.1', replicas: env.id === 'env-search-dev' ? 1 : 2 },
          ],
        },
      })
    }
  }

  private connection(id: string, kind: PortKind, provider: string, name: string, projectId: string | null, baseUrl: string | null, enabled: boolean, options: Record<string, unknown>, secretRef: string | null): Connection {
    return { id, kind, provider, name, project_id: projectId, base_url: baseUrl, enabled, options, secret_ref: secretRef, created_at: isoMinutesAgo(8_000), updated_at: isoMinutesAgo(25) }
  }

  private consumer(id: string, name: string, role: Consumer['role'], consumerType: Consumer['consumer_type'], enabled: boolean, fingerprint: string): Consumer {
    return { id, name, role, consumer_type: consumerType, enabled, scopes: [{ project_id: 'proj-alpha', app_id: null }], created_at: isoMinutesAgo(9_000), last_used_at: enabled ? isoMinutesAgo(12) : null, key_fingerprint: fingerprint, rotation_count: 0 }
  }

  private consumerCreated(id: string, name: string, role: Consumer['role'], consumerType: Consumer['consumer_type'], enabled: boolean, fingerprint: string): ConsumerCreated {
    return { ...this.consumer(id, name, role, consumerType, enabled, fingerprint), api_key: `apex_dummy_${fingerprint}` }
  }

  private document(id: string, name: string, mediaType: string, sizeBytes: number, summary: string): DocumentOut {
    return { id, name, media_type: mediaType, size_bytes: sizeBytes, artifact_key: `documents/${id}`, project_id: 'proj-alpha', app_id: null, summary, uploaded_by: 'dev', created_at: isoMinutesAgo(1_000) }
  }

  private draft(id: string, title: string, projectId: string | null, payload: Record<string, unknown>): DraftRead {
    return { id, title, project_id: projectId, payload, created_by: 'dev', created_at: isoMinutesAgo(2_000), updated_at: isoMinutesAgo(20) }
  }

  private makePrompts(created: string, updated: string): PromptRecord[] {
    return [
      this.prompt('prompt-story', 'phase', 'story_analysis/system', 'Story analysis system prompt', 'You are the story analysis phase operator.', created, updated, false),
      this.prompt('prompt-exec', 'phase', 'execution/system', 'Execution system prompt', 'Run the plan and report engine telemetry.', created, updated, false),
      this.prompt('prompt-report', 'phase', 'reporting/system', 'Reporting system prompt', 'Summarize KPI deltas and release risk.', created, updated, false),
      this.prompt('prompt-app-checkout', 'application', 'app-checkout', 'Checkout application prompt', 'Checkout-specific requirements: preserve carts through payment retries, watch gateway 5xx rates, and report p95 latency for cart and payment APIs.', created, updated, false),
      this.prompt('prompt-app-billing', 'application', 'app-billing', 'Billing application prompt', 'Billing-specific requirements: validate export completion, ledger consistency, and invoice API p95 latency.', created, updated, false),
      this.prompt('prompt-old', 'experimental', 'legacy/reporting', 'Archived draft prompt', 'Old archived prompt body.', created, updated, true),
    ]
  }

  private prompt(id: string, namespace: string, key: string, description: string, content: string, created: string, updated: string, archived: boolean): PromptRecord {
    const v1: PromptVersionDetail = { id: `${id}-v1`, version: 1, content, note: 'initial dummy version', created_by: 'dev', created_at: created, parent_version_id: null }
    const v2: PromptVersionDetail = { id: `${id}-v2`, version: 2, content: `${content}\nBe concise and cite evidence.`, note: 'tighten output', created_by: 'dev', created_at: updated, parent_version_id: v1.id }
    return { id, namespace, key, description, content: v2.content, note: v2.note, active_version: { id: v2.id, version: 2 }, archived_at: archived ? isoMinutesAgo(300) : null, updated_at: updated, versions: [v1, v2] }
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
    return [
      { at: isoMinutesAgo(3), level: 'ERROR', service: 'payments-gateway', message: 'gateway 502 spike during checkout ramp', fields: { thread_id: 'run-failed', attempt: 2 } },
      { at: isoMinutesAgo(5), level: 'WARN', service: 'checkout-api', message: 'p95 latency crossed warning threshold', fields: { thread_id: 'run-busy' } },
      { at: isoMinutesAgo(8), level: 'INFO', service: 'apex-worker', message: 'engine poll collected live stats', fields: { external_run_id: 'al-dev-42' } },
      { at: isoMinutesAgo(13), level: 'DEBUG', service: 'apex-api', message: 'resume gate request accepted', fields: { interrupt_id: 'int-report' } },
    ]
  }

  private makeAssistants(created: string, updated: string): Assistant[] {
    return [
      this.assistant('pipeline', 'pipeline', 'System default', null, {}, { created_by: 'system' }, 1, created, updated),
      this.assistant('asst-release', 'pipeline', 'Release Gate Soak', 'Full gated release-candidate soak profile.', { project_id: 'proj-alpha', limits: { poll_interval_s: 5 } }, { created_by: 'dev' }, 3, created, updated),
      this.assistant('asst-fast', 'pipeline', 'Fast Smoke', 'Short smoke test for UI iteration.', { project_id: 'proj-beta', phases: ['story_analysis', 'execution', 'reporting'] }, { created_by: 'dev' }, 2, created, updated),
    ]
  }

  private assistant(id: string, graphId: string, name: string, description: string | null, configurable: Record<string, unknown>, metadata: Record<string, unknown>, version: number, created: string, updated: string): Assistant {
    return { assistant_id: id, graph_id: graphId, name, description: description ?? undefined, config: { configurable }, context: {}, metadata, version, created_at: created, updated_at: updated }
  }

  private seedPipelines(): void {
    const rows: Array<[string, string, string, 'busy' | 'interrupted' | 'idle' | 'error', PhaseName, 'prompt' | 'phase' | null]> = [
      ['run-busy', 'Checkout latency regression', 'proj-alpha', 'busy', 'execution', null],
      ['run-gated-prompt', 'Nightly soak prompt review', 'proj-alpha', 'interrupted', 'test_planning', 'prompt'],
      ['run-gated-report', 'Report approval for search rollout', 'proj-alpha', 'interrupted', 'reporting', 'phase'],
      ['run-succeeded', 'Search API release soak', 'proj-alpha', 'idle', 'postmortem', null],
      ['run-failed', 'Gateway failure rehearsal', 'proj-alpha', 'error', 'postmortem', null],
      ['run-beta-idle', 'Billing smoke baseline', 'proj-beta', 'idle', 'reporting', null],
    ]
    for (const [id, title, project, status, phase, gate] of rows) {
      const detail = this.pipelineDetail(id, title, project, status, phase)
      if (gate === 'prompt') this.addPromptGate(detail)
      if (gate === 'phase') this.addPhaseGate(detail)
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
    }
  }

  private pipelineDetail(id: string, title: string, projectId: string | undefined, status: 'busy' | 'interrupted' | 'idle' | 'error', phase: PhaseName): PipelineDetail {
    const state = makePipelineState(title, phase, status)
    return {
      thread_id: id,
      title,
      project_id: projectId ?? 'proj-alpha',
      app_id: projectId === 'proj-beta' ? 'app-billing' : 'app-checkout',
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
      engine: { engine: 'apexload', external_run_id: 'al-dev-42' },
      created_at: isoMinutesAgo(status === 'busy' ? 65 : 180),
      updated_at: status === 'interrupted' ? isoMinutesAgo(24) : isoMinutesAgo(status === 'busy' ? 2 : 45),
      pending_gate: null,
      values: state as Record<string, unknown>,
      interrupts: [],
    }
  }

  private addPromptGate(detail: PipelineDetail): void {
    detail.pending_gate = { interrupt_id: 'int-prompt', kind: 'prompt_review', phase: 'test_planning' }
    detail.interrupts = [
      {
        interrupt_id: 'int-prompt',
        kind: 'prompt_review',
        phase: 'test_planning',
        payload: {
          schema_version: 1,
          kind: 'prompt_review',
          phase: 'test_planning',
          prompt: {
            system: 'You are the test planning phase operator.',
            user: 'Plan a checkout soak focused on retry behavior.',
            source: { origin: 'catalog', ref: 'phase/test_planning/system@v2' },
          },
          context_packets: [{ id: 'ctx-1', source: 'jira', title: 'PHX-101 acceptance criteria', summary: 'Retry behavior under load.' }],
          tools: ['jira.lookup', 'documents.search'],
          editable: true,
          actions: ['approve', 'modify', 'skip_phase', 'abort'],
        },
      },
    ]
  }

  private addPhaseGate(detail: PipelineDetail): void {
    detail.pending_gate = { interrupt_id: 'int-report', kind: 'phase_review', phase: 'reporting' }
    detail.interrupts = [
      {
        interrupt_id: 'int-report',
        kind: 'phase_review',
        phase: 'reporting',
        payload: {
          schema_version: 1,
          kind: 'phase_review',
          phase: 'reporting',
          summary: 'Draft report compiled; KPI deltas are inside tolerance except search replica saturation.',
          result_preview: { summary: 'Release gate passes with monitoring note.', reasoning_digest: 'Search replica count is the main risk.' },
          artifacts: [{ id: 'exec-report', kind: 'report', name: 'load-report.json' }],
          warnings: ['Search replica count should be raised before production.'],
          dialogue_tail: [],
          actions: ['approve', 'revise', 'discuss', 'abort'],
        },
      },
    ]
  }

  private seedArtifacts(): void {
    this.artifacts.set('reports/exec-report', {
      mediaType: 'application/json',
      body: JSON.stringify({ engine: 'apexload', kpis: { tps_avg: 148.2, p95_ms: 312, error_rate: 0.006, vusers_peak: 450 } }, null, 2),
    })
    this.artifacts.set('reports/execution-transcript', {
      mediaType: 'text/plain',
      body: 'Execution started\nEngine provisioned\nPeak load reached\nReport generated\n',
    })
    this.artifacts.set('reports/results-archive', {
      mediaType: 'application/octet-stream',
      body: new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x00, 0x01]),
    })
  }

  private toSummary(detail: PipelineDetail): PipelineSummary {
    const { thread_id, title, project_id, app_id, thread_status, current_phase, phase_strip, engine, created_at, updated_at, pending_gate } = detail
    return { thread_id, title, project_id, app_id, thread_status, current_phase, phase_strip, engine, created_at, updated_at, pending_gate }
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
    const from = params.get('from') ?? isoMinutesAgo(bucket === 'hour' ? 8 * 60 : 7 * 1_440)
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
        const runScale = detail.project_id === 'proj-beta' ? 0.58 : 0.92 + runIndex * 0.07
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
          at: isoMinutesAgo(minutesAgo),
          thread_id: detail.thread_id,
          thread_title: detail.title ?? detail.thread_id,
          project_id: detail.project_id ?? null,
          phase,
          agent_name: `${phase}.worker`,
          model: metric.model,
          provider: 'anthropic',
          status: detail.thread_id === 'run-failed' && (phase === 'execution' || phase === 'reporting') ? 'error' : 'ok',
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
    const buckets = Array.from({ length: bucket === 'hour' ? 8 : 7 }, (_, index) => ({
      bucket_start: bucket === 'hour' ? isoMinutesAgo((7 - index) * 60) : isoMinutesAgo((6 - index) * 1_440),
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
    return { entries: page.items, total: rows.length, limit: page.limit, offset: page.offset, window: { from: body.window?.from ?? isoMinutesAgo(60), to: body.window?.to ?? nowIso() } }
  }

  private workItemPage(rows: WorkItem[], limit: number, offset: number): WorkItemPage {
    return { items: rows.slice(offset, offset + limit), total: rows.length, page: { limit, offset } }
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
