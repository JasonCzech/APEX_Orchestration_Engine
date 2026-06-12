/**
 * Central query-key factory (plan Part 2 — Data layer).
 * Every server-state read keys through here so SSE patches and invalidations
 * target a single source of truth.
 */
export const queryKeys = {
  system: {
    all: ['system'] as const,
    info: () => [...queryKeys.system.all, 'info'] as const,
  },
  pipelines: {
    all: ['pipelines'] as const,
    list: (filters: Record<string, unknown> = {}) =>
      [...queryKeys.pipelines.all, 'list', filters] as const,
    /** Prefix matching every list(filters) entry — SSE patches fan out here. */
    lists: () => [...queryKeys.pipelines.all, 'list'] as const,
    detail: (threadId: string) => [...queryKeys.pipelines.all, 'detail', threadId] as const,
  },
  threads: {
    all: ['threads'] as const,
    state: (threadId: string) => [...queryKeys.threads.all, threadId, 'state'] as const,
    /** Active (running/pending) run id discovery for the live stream (D2). */
    activeRun: (threadId: string) => [...queryKeys.threads.all, threadId, 'active-run'] as const,
    artifact: (threadId: string, artifactId: string) =>
      [...queryKeys.threads.all, threadId, 'artifact', artifactId] as const,
  },
  approvals: {
    all: ['approvals'] as const,
    inbox: () => [...queryKeys.approvals.all, 'inbox'] as const,
  },
  prompts: {
    all: ['prompts'] as const,
    list: () => [...queryKeys.prompts.all, 'list'] as const,
    detail: (ns: string, name: string) => [...queryKeys.prompts.all, ns, name] as const,
    versions: (ns: string, name: string) =>
      [...queryKeys.prompts.all, ns, name, 'versions'] as const,
    version: (ns: string, name: string, v: string | number) =>
      [...queryKeys.prompts.all, ns, name, 'versions', String(v)] as const,
    /** D4 append: list filtered to one namespace (object element ≠ ns/name keys). */
    listNamespace: (ns: string) => [...queryKeys.prompts.all, 'list', { ns }] as const,
    /** D4 append: prompt fetched by catalog id (GET /v1/prompts/{prompt_id}). */
    byId: (id: string) => [...queryKeys.prompts.all, 'id', id] as const,
  },
  catalog: {
    all: ['catalog'] as const,
    applications: () => [...queryKeys.catalog.all, 'applications'] as const,
    environments: () => [...queryKeys.catalog.all, 'environments'] as const,
    environment: (id: string) => [...queryKeys.catalog.all, 'environments', id] as const,
    /** D4 append: applications filtered by project (?project=). */
    applicationsBy: (project?: string) =>
      [...queryKeys.catalog.applications(), { project: project ?? null }] as const,
    /** D4 append: environments filtered by application (?application=). */
    environmentsBy: (application?: string | null) =>
      [...queryKeys.catalog.environments(), { application: application ?? null }] as const,
  },
  workItems: {
    all: ['work-items'] as const,
    savedQueries: () => [...queryKeys.workItems.all, 'saved-queries'] as const,
    item: (provider: string, itemId: string) =>
      [...queryKeys.workItems.all, provider, itemId] as const,
    /** D4 append: provider-less lookup by key (GET /v1/work-tracking/items/{key}). */
    key: (key: string) => [...queryKeys.workItems.all, 'key', key] as const,
  },
  context: {
    all: ['context'] as const,
    summaries: () => [...queryKeys.context.all, 'summaries'] as const,
    evidence: (filters: Record<string, unknown> = {}) =>
      [...queryKeys.context.all, 'evidence', filters] as const,
  },
  analytics: {
    all: ['analytics'] as const,
    usage: (params: Record<string, unknown> = {}) =>
      [...queryKeys.analytics.all, 'usage', params] as const,
  },
  logs: {
    all: ['logs'] as const,
    search: (params: Record<string, unknown> = {}) =>
      [...queryKeys.logs.all, 'search', params] as const,
  },
  engines: {
    all: ['engines'] as const,
    runs: () => [...queryKeys.engines.all, 'runs'] as const,
    run: (threadId: string) => [...queryKeys.engines.all, 'runs', threadId] as const,
  },
  goldenConfigs: {
    all: ['golden-configs'] as const,
    list: () => [...queryKeys.goldenConfigs.all, 'list'] as const,
    detail: (assistantId: string) => [...queryKeys.goldenConfigs.all, assistantId] as const,
  },
  admin: {
    all: ['admin'] as const,
    connections: () => [...queryKeys.admin.all, 'connections'] as const,
    connection: (id: string) => [...queryKeys.admin.all, 'connections', id] as const,
    consumers: () => [...queryKeys.admin.all, 'consumers'] as const,
    consumer: (id: string) => [...queryKeys.admin.all, 'consumers', id] as const,
  },
  documents: {
    all: ['documents'] as const,
    list: () => [...queryKeys.documents.all, 'list'] as const,
    detail: (id: string) => [...queryKeys.documents.all, id] as const,
    /** D4 append: list filtered by project (?project=). */
    listBy: (project?: string) => [...queryKeys.documents.list(), { project: project ?? null }] as const,
  },
  /** D4 append: server-side wizard drafts (/v1/drafts). */
  drafts: {
    all: ['drafts'] as const,
    list: (project?: string) => [...queryKeys.drafts.all, 'list', { project: project ?? null }] as const,
    detail: (id: string) => [...queryKeys.drafts.all, id] as const,
  },
}

/** Stale times per plan: catalog/prompts/admin 60s, pipelines list 15s, thread state 0. */
export const STALE_TIMES = {
  catalog: 60_000,
  prompts: 60_000,
  admin: 60_000,
  pipelinesList: 15_000,
  threadState: 0,
  systemInfo: 30_000,
} as const
