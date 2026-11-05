/**
 * Offline AIFx Context Services fixtures for demo mode: one onboarded
 * organization, the ACC and OPUS applications mapped to their APM
 * repositories, and the deterministic preview / pinned snapshot the backend
 * stub would produce.
 */
import type { AifxPreview, PipelineAifx } from '@/api/hooks/useAifx'
import type { AifxGatePreview, PhaseName } from '@apex/pipeline-events'

export const AIFX_DEMO_CONNECTION_ID = 'conn-ghe'
export const AIFX_DEMO_ORG_ID = 'aifx-org-cm'
const OWNER = 'aifx'
const WEB = 'https://ghe.example.test'

type Role = 'global-rules' | 'team-documents' | 'application-context'
type EntryType = 'rule' | 'memory' | 'skill' | 'context'

interface DemoEntry {
  id: string
  type: EntryType
  title: string
  path: string
  phases: PhaseName[] | 'all'
  description?: string
  content: string
}

const GLOBAL_ENTRIES: DemoEntry[] = [
  { id: 'evidence-first', type: 'rule', title: 'Evidence-first performance engineering', path: 'rules/evidence-first.md', phases: 'all', content: '# Evidence-first performance engineering\n\n- Every target, threshold and workload share must cite its evidence source.\n- Mark anything without evidence as UNKNOWN and raise it as a blocker.' },
  { id: 'workload-modeling', type: 'skill', title: 'Workload modeling', path: 'skills/workload-modeling.md', phases: ['test_planning', 'script_scenario'], description: 'Derive arrival rates, pacing and think time from production traffic evidence.', content: '# Workload modeling\n\n1. Start from peak-hour transaction counts in production telemetry.\n2. Convert to arrival rates per transaction and check the shares total 100%.' },
  { id: 'prior-cycle-lessons', type: 'memory', title: 'Lessons from earlier cycles', path: 'memories/prior-cycle-lessons.md', phases: ['test_planning', 'reporting', 'postmortem'], content: '# Lessons from earlier cycles\n\n- Checkout p95 regressed twice after cache-tier changes; include cache warm-up in ramp.' },
]
const TEAM_ENTRIES: DemoEntry[] = [
  { id: 'team-standards', type: 'rule', title: 'Team performance standards', path: 'rules/team-standards.md', phases: 'all', content: '# Team performance standards\n\n- Report p95 and p99 latency for every transaction, not averages alone.' },
  { id: 'escalation', type: 'context', title: 'Escalation and ownership', path: 'context/escalation.md', phases: ['env_triage', 'reporting', 'postmortem'], content: '# Escalation and ownership\n\n- Environment issues go to the platform team’s queue before the test resumes.' },
]
const ACB_ENTRIES: Record<string, DemoEntry[]> = {
  ACC: [
    { id: 'architecture', type: 'context', title: 'ACC architecture', path: 'context/architecture.md', phases: 'all', content: '# ACC architecture\n\n- ACC calls Mule process APIs only; it never calls IDP or BSSe directly.\n- Order submit fans out from Mule to IDP fulfillment over IBM MQ.' },
    { id: 'known-limits', type: 'memory', title: 'Known capacity limits', path: 'memories/known-limits.md', phases: ['test_planning', 'env_triage', 'reporting'], content: '# Known capacity limits\n\n- IDP fulfillment saturates with fewer than four MQ consumers on PERFHYPER2.' },
  ],
  OPUS: [
    { id: 'architecture', type: 'context', title: 'OPUS architecture', path: 'context/architecture.md', phases: 'all', content: '# OPUS architecture\n\n- OPUS calls CSI services only; CSI fans out to CAS, BOBPM and TLG.\n- PERF TLG is shared by T3, T4 and T5, so concurrent tests contend for it.' },
    { id: 'known-limits', type: 'memory', title: 'Known capacity limits', path: 'memories/known-limits.md', phases: ['test_planning', 'env_triage', 'reporting'], content: '# Known capacity limits\n\n- CSI order submit p95 doubles once TLG connection-pool waits exceed 50 ms.' },
  ],
}

function acbEntries(applicationName: string | null): DemoEntry[] {
  const name = applicationName ?? 'Application'
  return ACB_ENTRIES[name] ?? [
    { id: 'architecture', type: 'context', title: `${name} architecture`, path: 'context/architecture.md', phases: 'all', content: `# ${name} architecture\n\n- Describe the services this application calls and the invariants a test must keep.` },
  ]
}

function sha(seed: string, length: number): string {
  let hash = 0x811c9dc5
  let out = ''
  for (let round = 0; out.length < length; round += 1) {
    for (const char of `${seed}:${round}`) {
      hash ^= char.charCodeAt(0)
      hash = Math.imul(hash, 0x01000193) >>> 0
    }
    out += hash.toString(16).padStart(8, '0')
  }
  return out.slice(0, length)
}

export interface DemoAifxOrganization {
  id: string
  name: string
  connection_id: string
  ghe_owner: string
  global_rules_repo: string | null
  team_documents_repo_pattern: string | null
  acb_repo_pattern: string | null
  default_ref: string
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface DemoAifxSource {
  application_id: string
  application_name: string | null
  organization_id: string
  apm_id: string
  team_documents_repo: string | null
  acb_repo: string | null
  ref: string | null
  enabled: boolean
  updated_at: string | null
}

export function demoAifxOrganizations(at: string): DemoAifxOrganization[] {
  return [
    {
      id: AIFX_DEMO_ORG_ID,
      name: 'Consumer & Mobility',
      connection_id: AIFX_DEMO_CONNECTION_ID,
      ghe_owner: OWNER,
      global_rules_repo: 'global-rules',
      team_documents_repo_pattern: '{apm}-team-documents',
      acb_repo_pattern: '{apm}-acb',
      default_ref: 'main',
      enabled: true,
      created_at: at,
      updated_at: at,
    },
  ]
}

const DEMO_APM_IDS: Record<string, string> = { ACC: 'APM0012345', OPUS: 'APM0067890' }

/** Mappings for the demo applications that are onboarded (ACC and OPUS). */
export function demoAifxSources(applications: { id: string; name: string }[], at: string): DemoAifxSource[] {
  return applications.flatMap((app) => {
    const apm = DEMO_APM_IDS[app.name]
    if (!apm) return []
    return [
      {
        application_id: app.id,
        application_name: app.name,
        organization_id: AIFX_DEMO_ORG_ID,
        apm_id: apm,
        team_documents_repo: `${apm.toLowerCase()}-team-documents`,
        acb_repo: `${apm.toLowerCase()}-acb`,
        ref: null,
        enabled: true,
        updated_at: at,
      },
    ]
  })
}

interface PlannedSource {
  role: Role
  repo: string
  ref: string
  applicationId: string | null
  applicationName: string | null
  entries: DemoEntry[]
}

type PlanSource = Pick<DemoAifxSource, 'application_id' | 'application_name' | 'team_documents_repo' | 'acb_repo' | 'ref'>

/**
 * Global rules come from the organization and team documents from the primary
 * system (``sources[0]``, when it is onboarded); ACB comes from every onboarded
 * participant, read once per repository.
 */
function plan(org: DemoAifxOrganization, sources: PlanSource[], primaryOnboarded = true): PlannedSource[] {
  const [primary] = sources
  if (!primary) return []
  const planned: PlannedSource[] = []
  if (org.global_rules_repo) planned.push({ role: 'global-rules', repo: `${org.ghe_owner}/${org.global_rules_repo}`, ref: org.default_ref, applicationId: null, applicationName: null, entries: GLOBAL_ENTRIES })
  if (primaryOnboarded && primary.team_documents_repo) planned.push({ role: 'team-documents', repo: `${org.ghe_owner}/${primary.team_documents_repo}`, ref: primary.ref || org.default_ref, applicationId: primary.application_id, applicationName: primary.application_name, entries: TEAM_ENTRIES })
  for (const source of sources) {
    const repo = source.acb_repo && `${org.ghe_owner}/${source.acb_repo}`
    if (!repo || planned.some((item) => item.role === 'application-context' && item.repo === repo)) continue
    planned.push({ role: 'application-context', repo, ref: source.ref || org.default_ref, applicationId: source.application_id, applicationName: source.application_name, entries: acbEntries(source.application_name) })
  }
  return planned
}

export function demoAifxPreview(org: DemoAifxOrganization, source: PlanSource): AifxPreview {
  const sources = plan(org, [source]).map((item) => {
    const commit = sha(`${item.repo}@${item.ref}`, 40)
    return {
      role: item.role,
      repo: item.repo,
      ref: item.ref,
      ok: true,
      commit_sha: commit,
      web_url: `${WEB}/${item.repo}/tree/${commit}`,
      application_id: item.applicationId,
      entries: item.entries.map((entry) => ({ id: entry.id, type: entry.type, title: entry.title, path: entry.path, phases: entry.phases, chars: entry.content.length })),
    }
  })
  return {
    ok: sources.length > 0,
    sources,
    issues: [],
    total_chars: sources.reduce((sum, item) => sum + item.entries.reduce((inner, entry) => inner + entry.chars, 0), 0),
  }
}

export function demoPipelineAifx(org: DemoAifxOrganization, mappings: DemoAifxSource[], primaryOnboarded = true): PipelineAifx {
  const planned = plan(org, mappings, primaryOnboarded)
  const sources = planned.map((item) => {
    const commit = sha(`${item.repo}@${item.ref}`, 40)
    return {
      role: item.role,
      application_id: item.applicationId,
      application_name: item.applicationName,
      repo: item.repo,
      ref: item.ref,
      commit_sha: commit,
      manifest_sha256: sha(`${item.repo}:manifest`, 64),
      web_url: `${WEB}/${item.repo}/tree/${commit}`,
    }
  })
  const entries = planned.flatMap((item) =>
    item.entries.map((entry) => ({
      id: entry.id,
      role: item.role,
      application_id: item.applicationId,
      repo: item.repo,
      type: entry.type,
      phases: entry.phases,
      title: entry.title,
      description: entry.description ?? null,
      path: entry.path,
      content_sha256: sha(entry.content, 64),
      content: entry.content,
    })),
  )
  return {
    status: 'pinned',
    snapshot: { schema_version: 1, status: 'pinned', sources, entries, issues: [], snapshot_sha256: sha(JSON.stringify(entries.map((e) => e.id)), 64) },
  } as PipelineAifx
}

export function demoGateAifx(pinned: PipelineAifx, phase: string): AifxGatePreview | undefined {
  const snapshot = pinned.snapshot
  if (!snapshot) return undefined
  const applicable = (snapshot.entries ?? []).filter(
    (entry) => entry.phases === 'all' || (Array.isArray(entry.phases) && entry.phases.includes(phase as PhaseName)),
  )
  return {
    status: 'pinned',
    snapshot_sha256: snapshot.snapshot_sha256,
    sources: (snapshot.sources ?? []).map((source) => ({
      role: source.role as Role,
      repo: source.repo,
      ref: source.ref,
      commit_sha: source.commit_sha,
      web_url: source.web_url ?? null,
      application_name: source.application_name ?? null,
    })),
    entries: applicable.map((entry) => ({ id: entry.id, type: entry.type as EntryType, title: entry.title, repo: entry.repo, path: entry.path })),
    issues: [],
    entry_count: applicable.length,
    issue_count: 0,
  }
}
