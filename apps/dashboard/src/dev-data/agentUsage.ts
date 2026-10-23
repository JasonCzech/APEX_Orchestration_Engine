// Offline demo model catalog and agent usage history.
// Prices are illustrative list prices (USD per million tokens) for demo purposes only;
// confirm against each provider's rate card before using them for real estimates.
import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'

import { demoAppId, type DemoAppCode } from './telecomCatalog'

const DAY_MS = 86_400_000
const MINUTE_MS = 60_000

export interface DemoPrice {
  input: number
  output: number
  cacheRead: number
  cacheCreate: number
}

export interface DemoPriceGeneration {
  number: number
  /** Days before demo activation when this price took effect. */
  effectiveDaysAgo: number
  reason: string
  source: string
  price: DemoPrice
}

export interface DemoModel {
  id: string
  model_key: string
  display_name: string
  provider: string
  /** Days before activation the model was onboarded. */
  addedDaysAgo: number
  /** Days before activation the model was archived; null while available. */
  archivedDaysAgo: number | null
  archiveReason: string | null
  /** Relative speed: latency multiplier against the phase baseline. */
  latency: number
  /** Share of output spent on reasoning tokens. */
  reasoning: number
  generations: DemoPriceGeneration[]
}

const gen = (number: number, effectiveDaysAgo: number, price: DemoPrice, reason = 'Published list price at onboarding', source = 'provider_rate_card'): DemoPriceGeneration => ({ number, effectiveDaysAgo, reason, source, price })

export const DEMO_MODELS: DemoModel[] = [
  { id: 'mdl-claude-fable-5-1', model_key: 'claude-fable-5-1', display_name: 'Claude Fable 5.1', provider: 'anthropic', addedDaysAgo: 12, archivedDaysAgo: null, archiveReason: null, latency: 1.9, reasoning: 0.22, generations: [gen(1, 12, { input: 10, output: 50, cacheRead: 1, cacheCreate: 12.5 })] },
  { id: 'mdl-claude-opus-5-5', model_key: 'claude-opus-5-5', display_name: 'Claude Opus 5.5', provider: 'anthropic', addedDaysAgo: 38, archivedDaysAgo: null, archiveReason: null, latency: 1.35, reasoning: 0.16, generations: [gen(1, 38, { input: 5, output: 25, cacheRead: 0.5, cacheCreate: 6.25 }), gen(2, 16, { input: 4, output: 20, cacheRead: 0.2, cacheCreate: 5 }, 'Provider list-price reduction')] },
  { id: 'mdl-claude-sonnet-5-5', model_key: 'claude-sonnet-5-5', display_name: 'Claude Sonnet 5.5', provider: 'anthropic', addedDaysAgo: 38, archivedDaysAgo: null, archiveReason: null, latency: 0.85, reasoning: 0.1, generations: [gen(1, 38, { input: 2, output: 10, cacheRead: 0.2, cacheCreate: 2.5 })] },
  { id: 'mdl-claude-haiku-5-5', model_key: 'claude-haiku-5-5', display_name: 'Claude Haiku 5.5', provider: 'anthropic', addedDaysAgo: 38, archivedDaysAgo: null, archiveReason: null, latency: 0.4, reasoning: 0.03, generations: [gen(1, 38, { input: 0.1, output: 0.5, cacheRead: 0.01, cacheCreate: 0.125 })] },
  { id: 'mdl-gpt-6-1-sol', model_key: 'gpt-6.1-sol', display_name: 'GPT-6.1 Sol', provider: 'openai', addedDaysAgo: 27, archivedDaysAgo: null, archiveReason: null, latency: 1.05, reasoning: 0.2, generations: [gen(1, 27, { input: 2, output: 10, cacheRead: 0.1, cacheCreate: 2 })] },
  { id: 'mdl-gpt-6-luna', model_key: 'gpt-6-luna', display_name: 'GPT-6 Luna', provider: 'openai', addedDaysAgo: 27, archivedDaysAgo: null, archiveReason: null, latency: 0.45, reasoning: 0.05, generations: [gen(1, 27, { input: 0.1, output: 0.5, cacheRead: 0.01, cacheCreate: 0.1 })] },
  { id: 'mdl-gemini-3-1-pro', model_key: 'gemini-3.1-pro', display_name: 'Gemini 3.1 Pro', provider: 'google', addedDaysAgo: 27, archivedDaysAgo: null, archiveReason: null, latency: 1.1, reasoning: 0.18, generations: [gen(1, 27, { input: 2, output: 12, cacheRead: 0.2, cacheCreate: 2 })] },
  { id: 'mdl-gemini-3-8-flash', model_key: 'gemini-3.8-flash', display_name: 'Gemini 3.8 Flash', provider: 'google', addedDaysAgo: 20, archivedDaysAgo: null, archiveReason: null, latency: 0.5, reasoning: 0.06, generations: [gen(1, 20, { input: 0.75, output: 3.75, cacheRead: 0.075, cacheCreate: 0.75 }, 'Introductory price through 2026-12-31')] },
  { id: 'mdl-grok-4-5', model_key: 'grok-4.5', display_name: 'Grok 4.5', provider: 'xai', addedDaysAgo: 14, archivedDaysAgo: null, archiveReason: null, latency: 0.95, reasoning: 0.14, generations: [gen(1, 14, { input: 2, output: 6, cacheRead: 0.5, cacheCreate: 2 })] },
  { id: 'mdl-mistral-medium-3-5', model_key: 'mistral-medium-3.5', display_name: 'Mistral Medium 3.5', provider: 'mistral', addedDaysAgo: 14, archivedDaysAgo: null, archiveReason: null, latency: 0.8, reasoning: 0.04, generations: [gen(1, 14, { input: 1.5, output: 7.5, cacheRead: 0.15, cacheCreate: 1.5 })] },
  // Archived: the pre-October lineup. Usage and pricing history remain reviewable.
  { id: 'mdl-claude-opus-4', model_key: 'claude-opus-4-20250514', display_name: 'Claude Opus 4', provider: 'anthropic', addedDaysAgo: 180, archivedDaysAgo: 35, archiveReason: 'Superseded by Claude Opus 5.5; retained for usage and pricing history.', latency: 1.6, reasoning: 0.12, generations: [gen(1, 180, { input: 15, output: 75, cacheRead: 1.5, cacheCreate: 18.75 })] },
  { id: 'mdl-claude-sonnet-4', model_key: 'claude-sonnet-4-20250514', display_name: 'Claude Sonnet 4', provider: 'anthropic', addedDaysAgo: 180, archivedDaysAgo: 35, archiveReason: 'Superseded by Claude Sonnet 5.5; retained for usage and pricing history.', latency: 1, reasoning: 0.07, generations: [gen(1, 180, { input: 3, output: 15, cacheRead: 0.3, cacheCreate: 3.75 })] },
  { id: 'mdl-claude-haiku-3-5', model_key: 'claude-3-5-haiku-20241022', display_name: 'Claude Haiku 3.5', provider: 'anthropic', addedDaysAgo: 180, archivedDaysAgo: 35, archiveReason: 'Superseded by Claude Haiku 5.5; retained for usage and pricing history.', latency: 0.55, reasoning: 0, generations: [gen(1, 180, { input: 0.8, output: 4, cacheRead: 0.08, cacheCreate: 1 })] },
]

const MODEL_BY_KEY = new Map(DEMO_MODELS.map((model) => [model.model_key, model]))

export function demoModel(key: string): DemoModel | undefined {
  return MODEL_BY_KEY.get(key)
}

/** Price generation in effect at `at` (ms), relative to the activation anchor. */
export function demoPriceAt(model: DemoModel, atMs: number, anchorMs: number): DemoPriceGeneration | null {
  const daysAgo = (anchorMs - atMs) / DAY_MS
  const effective = model.generations.filter((generation) => generation.effectiveDaysAgo >= daysAgo)
  return effective.sort((left, right) => left.effectiveDaysAgo - right.effectiveDaysAgo)[0] ?? null
}

// ---------------------------------------------------------------------------
// Usage events
// ---------------------------------------------------------------------------

export interface DemoUsageEvent {
  id: string
  at: string
  thread_id: string
  thread_title: string
  project_id: string
  app_id: string
  consumer_id: string
  consumer_name_snapshot: string
  phase: PhaseName
  agent_name: string
  model: string
  model_catalog_id: string
  provider: string
  archived: boolean
  status: 'ok' | 'error'
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_creation_tokens: number
  reasoning_tokens: number
  total_tokens: number
  latency_ms: number
  cost_usd: number
  pricing_interval_id: string | null
}

export interface DemoHistoricalRun {
  thread_id: string
  title: string
  project_id: string
  app_id: string
  profile: 'acc-baseline' | 'npi-launch' | 'legacy-baseline' | 'digital-baseline' | 'hrp-baseline'
  endedAt: string
}

export interface DemoSeededRun {
  thread_id: string
  title: string
  project_id: string
  app_id: string
  /** Phases that ran, with minutes before activation that each started. */
  phases: Array<{ phase: PhaseName; startedAgo: number; failed?: boolean }>
}

const PHASE_BASE: Record<PhaseName, { input: number; output: number; cacheRead: number; cacheCreate: number; latency: number }> = {
  story_analysis: { input: 14_500, output: 2_600, cacheRead: 6_200, cacheCreate: 1_400, latency: 9_000 },
  test_planning: { input: 18_800, output: 3_900, cacheRead: 8_400, cacheCreate: 1_900, latency: 14_000 },
  env_triage: { input: 6_400, output: 1_100, cacheRead: 3_100, cacheCreate: 600, latency: 3_800 },
  script_scenario: { input: 21_000, output: 6_800, cacheRead: 9_500, cacheCreate: 2_100, latency: 19_000 },
  execution: { input: 1_900, output: 380, cacheRead: 900, cacheCreate: 200, latency: 1_600 },
  reporting: { input: 26_000, output: 5_400, cacheRead: 11_800, cacheCreate: 2_600, latency: 21_000 },
  postmortem: { input: 12_200, output: 2_300, cacheRead: 5_600, cacheCreate: 1_100, latency: 8_500 },
}

const PROJECT_SCALE: Record<string, number> = { 'acc-perf': 1, 'digital-perf': 0.7, 'legacy-perf': 1.15, npi: 1.3, hrp: 1.4 }

const LEGACY_LINEUP: Record<PhaseName, string> = {
  story_analysis: 'claude-sonnet-4-20250514',
  test_planning: 'claude-sonnet-4-20250514',
  env_triage: 'claude-3-5-haiku-20241022',
  script_scenario: 'claude-sonnet-4-20250514',
  execution: 'claude-3-5-haiku-20241022',
  reporting: 'claude-opus-4-20250514',
  postmortem: 'claude-sonnet-4-20250514',
}

const CURRENT_LINEUP: Record<PhaseName, string> = {
  story_analysis: 'claude-sonnet-5-5',
  test_planning: 'claude-opus-5-5',
  env_triage: 'claude-haiku-5-5',
  script_scenario: 'claude-sonnet-5-5',
  execution: 'claude-haiku-5-5',
  reporting: 'claude-opus-5-5',
  postmortem: 'claude-sonnet-5-5',
}

/** Per-project model evaluations layered on the current lineup once a model is onboarded. */
const PROJECT_OVERRIDES: Record<string, Partial<Record<PhaseName, string>>> = {
  'digital-perf': { story_analysis: 'gemini-3.1-pro', env_triage: 'gemini-3.8-flash', script_scenario: 'gpt-6.1-sol' },
  'legacy-perf': { script_scenario: 'mistral-medium-3.5', env_triage: 'gpt-6-luna', execution: 'gpt-6-luna' },
  npi: { test_planning: 'gpt-6.1-sol', reporting: 'claude-fable-5-1' },
  hrp: { story_analysis: 'grok-4.5', postmortem: 'gemini-3.1-pro', reporting: 'claude-fable-5-1' },
}

const CONSUMERS: Record<string, Array<[string, string]>> = {
  'acc-perf': [['cons-perf-priya', 'Priya Raman'], ['cons-operator', 'Performance Operator'], ['cons-admin', 'Demo Presenter']],
  'digital-perf': [['cons-perf-elena', 'Elena Ortiz'], ['cons-operator', 'Performance Operator']],
  'legacy-perf': [['cons-perf-marcus', 'Marcus Webb'], ['cons-operator', 'Performance Operator']],
  npi: [['cons-perf-sam', 'Sam Okafor'], ['cons-admin', 'Demo Presenter']],
  hrp: [['cons-perf-sam', 'Sam Okafor'], ['cons-perf-marcus', 'Marcus Webb']],
}

const HISTORICAL_TITLES: Record<string, Array<[string, DemoHistoricalRun['profile'], string]>> = {
  'acc-perf': [
    ['ACC add-a-line regression — PERFHYPER2', 'acc-baseline', 'acc'],
    ['ACC upgrade-eligibility smoke — PERFHYPER1', 'acc-baseline', 'acc'],
    ['ACC add-a-line release candidate — PERFHYPER2', 'acc-baseline', 'acc'],
  ],
  'digital-perf': [
    ['Mule order-status API soak — PERFHYPER1', 'digital-baseline', 'mule'],
    ['Mule account-summary API load — PERFHYPER3', 'digital-baseline', 'mule'],
  ],
  'legacy-perf': [
    ['OPUS device upgrade store peak — T4', 'legacy-baseline', 'opus'],
    ['OPUS activation with CAS credit — T5', 'legacy-baseline', 'opus'],
  ],
  npi: [['NPI promo eligibility rehearsal — PERFHYPER3', 'npi-launch', 'acc']],
  hrp: [['HRP peak headroom — OPUS on T3', 'hrp-baseline', 'opus']],
}

/** Weekday run cadence per project (runs per business day). */
const CADENCE: Record<string, number> = { 'acc-perf': 3, 'digital-perf': 2, 'legacy-perf': 2, npi: 1, hrp: 1 }

function mulberry32(seed: number): () => number {
  let state = seed >>> 0
  return () => {
    state = (state + 0x6d2b79f5) >>> 0
    let t = state
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4_294_967_296
  }
}

export const DEMO_USAGE_HISTORY_DAYS = 90

function modelFor(project: string, phase: PhaseName, daysAgo: number): DemoModel {
  if (daysAgo > 35) return MODEL_BY_KEY.get(LEGACY_LINEUP[phase])!
  const override = PROJECT_OVERRIDES[project]?.[phase]
  const candidate = override ? MODEL_BY_KEY.get(override) : undefined
  if (candidate && candidate.addedDaysAgo >= daysAgo) return candidate
  const current = MODEL_BY_KEY.get(CURRENT_LINEUP[phase])!
  return current.addedDaysAgo >= daysAgo ? current : MODEL_BY_KEY.get(LEGACY_LINEUP[phase])!
}

function makeEvent(
  random: () => number,
  anchorMs: number,
  atMs: number,
  run: { thread_id: string; title: string; project_id: string; app_id: string },
  phase: PhaseName,
  consumer: [string, string],
  sequence: number,
  forceError = false,
): DemoUsageEvent {
  const daysAgo = (anchorMs - atMs) / DAY_MS
  const model = modelFor(run.project_id, phase, daysAgo)
  const base = PHASE_BASE[phase]
  const scale = (PROJECT_SCALE[run.project_id] ?? 1) * (0.75 + random() * 0.55)
  const error = forceError || random() < 0.018
  const input = Math.round(base.input * scale)
  const output = error ? 0 : Math.round(base.output * scale * (0.85 + random() * 0.3))
  const cacheRead = Math.round(base.cacheRead * scale * (sequence > 0 ? 1.1 : 0.6))
  const cacheCreate = Math.round(base.cacheCreate * scale * (sequence > 0 ? 0.4 : 1))
  const reasoning = Math.round(output * model.reasoning)
  const generation = demoPriceAt(model, atMs, anchorMs)
  const price = generation?.price
  const cost = price ? (input * price.input + output * price.output + cacheRead * price.cacheRead + cacheCreate * price.cacheCreate) / 1_000_000 : 0
  const latency = error ? Math.round(30_000 + random() * 30_000) : Math.round(base.latency * model.latency * (0.7 + random() * 0.7))
  return {
    id: `evt-${run.thread_id}-${phase}-${sequence}`,
    at: new Date(atMs).toISOString(),
    thread_id: run.thread_id,
    thread_title: run.title,
    project_id: run.project_id,
    app_id: run.app_id,
    consumer_id: consumer[0],
    consumer_name_snapshot: consumer[1],
    phase,
    agent_name: `${phase}.worker`,
    model: model.model_key,
    model_catalog_id: model.id,
    provider: model.provider,
    archived: model.archivedDaysAgo !== null,
    status: error ? 'error' : 'ok',
    input_tokens: input,
    output_tokens: output,
    cache_read_tokens: cacheRead,
    cache_creation_tokens: cacheCreate,
    reasoning_tokens: reasoning,
    total_tokens: input + output,
    latency_ms: latency,
    cost_usd: Number(cost.toFixed(6)),
    pricing_interval_id: generation ? `${model.id}-g${generation.number}` : null,
  }
}

/**
 * One fixed usage history independent of the requested window or bucket, so a
 * shorter window is always a subset of a longer one.
 */
export function buildDemoUsage(anchorIso: string, seeded: DemoSeededRun[]): { events: DemoUsageEvent[]; historicalRuns: DemoHistoricalRun[] } {
  const anchorMs = Date.parse(anchorIso)
  const random = mulberry32(0x5eed_a9e)
  const events: DemoUsageEvent[] = []
  const historicalRuns: DemoHistoricalRun[] = []
  const anchorDay = new Date(anchorMs)
  anchorDay.setUTCHours(0, 0, 0, 0)
  for (let day = DEMO_USAGE_HISTORY_DAYS; day >= 0; day -= 1) {
    const dayStart = anchorDay.getTime() - day * DAY_MS
    const weekday = new Date(dayStart).getUTCDay()
    const businessDay = weekday !== 0 && weekday !== 6
    // Nightly certification (headless) at 07:10 UTC on every day except Sunday.
    const plans: Array<{ project: string; startMs: number; consumer: [string, string] }> = []
    if (weekday !== 0) plans.push({ project: 'acc-perf', startMs: dayStart + 7 * 60 * MINUTE_MS + 10 * MINUTE_MS, consumer: ['cons-ci', 'Nightly Certification'] })
    if (businessDay) {
      for (const [project, cadence] of Object.entries(CADENCE)) {
        const count = Math.max(0, cadence + (random() < 0.3 ? 1 : 0) - (random() < 0.25 ? 1 : 0))
        for (let index = 0; index < count; index += 1) {
          const consumers = CONSUMERS[project]!
          plans.push({ project, startMs: dayStart + (13 + random() * 9) * 60 * MINUTE_MS, consumer: consumers[Math.floor(random() * consumers.length)]! })
        }
      }
    }
    plans.sort((left, right) => left.startMs - right.startMs)
    for (const [index, plan] of plans.entries()) {
      const titles = HISTORICAL_TITLES[plan.project]!
      const [title, profile, app] = titles[Math.floor(random() * titles.length)]!
      const stamp = new Date(dayStart).toISOString().slice(0, 10).replaceAll('-', '')
      const run = { thread_id: `hist-${stamp}-${plan.project}-${index + 1}`, title, project_id: plan.project, app_id: demoAppId(plan.project, app as DemoAppCode), profile }
      let cursor = plan.startMs
      const runEvents: DemoUsageEvent[] = []
      for (const phase of PHASE_NAMES) {
        if (cursor >= anchorMs) break
        const attempts = random() < 0.12 ? 2 : 1
        for (let attempt = 0; attempt < attempts; attempt += 1) {
          runEvents.push(makeEvent(random, anchorMs, cursor, run, phase, plan.consumer, attempt))
          // Gate review and execution time between model calls.
          cursor += (phase === 'execution' ? 35 : phase === 'env_triage' ? 6 : 4 + random() * 10) * MINUTE_MS
        }
      }
      if (!runEvents.length || cursor >= anchorMs - 5 * MINUTE_MS) continue
      events.push(...runEvents)
      historicalRuns.push({ thread_id: run.thread_id, title: run.title, project_id: run.project_id, app_id: run.app_id, profile: run.profile, endedAt: new Date(cursor).toISOString() })
    }
  }
  for (const run of seeded) {
    for (const [index, item] of run.phases.entries()) {
      const consumers = CONSUMERS[run.project_id] ?? CONSUMERS['acc-perf']!
      const atMs = anchorMs - item.startedAgo * MINUTE_MS + 40_000
      events.push(makeEvent(random, anchorMs, atMs, run, item.phase, consumers[0]!, index === 0 ? 0 : 1, item.failed))
    }
  }
  events.sort((left, right) => left.at.localeCompare(right.at) || left.id.localeCompare(right.id))
  return { events, historicalRuns }
}
