export const HELP_AREAS = [
  { id: 'getting-started', label: 'Getting started' },
  { id: 'user-guide', label: 'User guide' },
  { id: 'administration', label: 'Administration' },
  { id: 'api', label: 'API & automation' },
  { id: 'troubleshooting', label: 'Troubleshooting' },
] as const

export type HelpArea = typeof HELP_AREAS[number]['id']
export interface HelpArticle {
  id: string
  area: HelpArea
  title: string
  description: string
  keywords: string
  admin?: boolean
}

// This explicit catalog is the bundle allowlist. Operational notes and QA records
// are deliberately not swept into customer-facing help by a recursive glob.
export const helpArticles: readonly HelpArticle[] = [
  { id: 'welcome', area: 'getting-started', title: 'Concepts & navigation', description: 'Understand tests, runs, reviews, and the dashboard.', keywords: 'orientation home glossary pipeline thread gate' },
  { id: 'first-test', area: 'getting-started', title: 'Your first simulated test', description: 'Follow a test from setup through review and results.', keywords: 'quickstart demo tutorial onboarding simulation' },
  { id: 'scope', area: 'user-guide', title: 'Define test scope', description: 'Choose systems, environments, primary ownership, and traffic entry.', keywords: 'topology frontend middle backend participants target' },
  { id: 'configuration', area: 'user-guide', title: 'Configure workload & execution', description: 'Choose an engine, phase owners, workload, and acceptance criteria.', keywords: 'vusers duration ramp SLA thresholds ownership HITL model readiness' },
  { id: 'tickets', area: 'user-guide', title: 'Tickets & saved queries', description: 'Find requirements and select tickets for a test.', keywords: 'jira align jira cloud work tracking query defects' },
  { id: 'knowledge', area: 'user-guide', title: 'Knowledge & documents', description: 'Bring supporting material into the test context.', keywords: 'upload summary context packets sources documents' },
  { id: 'prompts', area: 'user-guide', title: 'Prompts & pinned versions', description: 'Select instructions and understand review-time revisions.', keywords: 'system phase application environment context defaults templates playground' },
  { id: 'drafts-presets', area: 'user-guide', title: 'Drafts, presets & launch', description: 'Save setup, reuse configurations, and launch a reviewed test.', keywords: 'autosave copy retries idempotency launch readiness golden configs' },
  { id: 'phases', area: 'user-guide', title: 'Run, review & complete phases', description: 'Work through all seven phases and record decisions.', keywords: 'approve revise discuss prompt review output review human completion HITL LangGraph' },
  { id: 'results', area: 'user-guide', title: 'Results, evidence & comparison', description: 'Interpret outcomes and inspect the evidence behind them.', keywords: 'artifacts timeline transcript acceptance unknown KPIs metrics compare baseline' },
  { id: 'analysis', area: 'user-guide', title: 'Reporting & analysis tools', description: 'Select analysis integrations and recover interrupted work.', keywords: 'LRA Engine PerfReportForge Other Tools internal RCA polling ledger recovery' },
  { id: 'cycles', area: 'user-guide', title: 'Manage test cycles', description: 'Version deliverables, review dependencies, and prepare executions.', keywords: 'scope plan readiness script report postmortem approval dispatch reservations followup' },
  { id: 'catalogs', area: 'user-guide', title: 'Applications & environments', description: 'Maintain catalog records, host inventory, and approved targets.', keywords: 'projects application environments host mappings target approval' },
  { id: 'apex-load', area: 'user-guide', title: 'Work with APEX Load', description: 'Understand execution bindings and cross-product navigation.', keywords: 'stormrunner native workload release mirrored runs import reconciliation handoff' },
  { id: 'analytics', area: 'user-guide', title: 'Analytics, pricing & logs', description: 'Investigate model usage, estimates, and operational events.', keywords: 'agent analytics tokens costs model pricing history logs filters' },
  { id: 'settings', area: 'user-guide', title: 'Preferences & session', description: 'Set appearance, timestamps, table defaults, and your API key.', keywords: 'theme dark solarized light monokai timezone sign out connection' },
  { id: 'access', area: 'administration', title: 'Roles, scopes & API consumers', description: 'Understand access and administer consumer credentials.', keywords: 'viewer operator admin authorization 401 403 key rotation revoke permissions', admin: true },
  { id: 'connections', area: 'administration', title: 'Configure connections', description: 'Set up engine, storage, work-tracking, and analysis integrations.', keywords: 'credentials probes providers connectivity project ownership secret', admin: true },
  { id: 'operations', area: 'administration', title: 'Deployment & operations', description: 'Configure the dashboard and maintain the running platform.', keywords: 'system status readiness health postgres redis backups restore HA SSE licensing AKS Helm Docker', admin: true },
  { id: 'api-workflows', area: 'api', title: 'Authentication & API workflows', description: 'Use the domain API and understand safe recovery.', keywords: 'x-api-key curl CLI automation LangGraph SDK pagination idempotency API' },
  { id: 'api-reference', area: 'api', title: 'Domain API reference', description: 'Search operations and inspect generated request and response schemas.', keywords: 'OpenAPI endpoints schema contract REST download swagger' },
  { id: 'troubleshooting', area: 'troubleshooting', title: 'Diagnose & recover', description: 'Resolve access, readiness, review, and integration problems.', keywords: 'errors blocked stale conflicts 409 retry timeout disconnected failure' },
  { id: 'capabilities', area: 'troubleshooting', title: 'Capabilities & limitations', description: 'Check simulation, integration, and evidence boundaries.', keywords: 'supported unavailable experimental legacy limitations' },
  { id: 'support', area: 'troubleshooting', title: 'Support, glossary & documentation', description: 'Collect useful evidence and understand terms and review scope.', keywords: 'contact support service desk build version changes owner glossary' },
]

export const HELP_REVIEW_DATE = '2026-10-24'
export const HELP_OWNER = 'APEX Orchestration maintainers'
export const helpHref = (id: string, section?: string) => `/help/${encodeURIComponent(id)}${section ? `#${encodeURIComponent(section)}` : ''}`
export const visibleArticles = (isAdmin: boolean) => helpArticles.filter(article => isAdmin || !article.admin)
