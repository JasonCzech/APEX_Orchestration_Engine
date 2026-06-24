/**
 * /analytics agent-behavior filter model + URLSearchParams serialization.
 * Defaults are omitted so deep links stay readable and back/forward safe.
 */

import { DAY_MS, HOUR_MS } from '@/components/controls/timeWindow'

import type { AgentOrder, AgentSort } from '@/api/hooks/useAgentAnalytics'

export const BUCKETS = ['hour', 'day'] as const
export type Bucket = (typeof BUCKETS)[number]

export const GROUP_BYS = ['model', 'stage', 'agent', 'test', 'date'] as const
export type GroupBy = (typeof GROUP_BYS)[number]

export const MEASURES = ['tokens', 'cost', 'latency'] as const
export type Measure = (typeof MEASURES)[number]

export const DEFAULT_GROUP: GroupBy = 'model'
export const DEFAULT_MEASURE: Measure = 'tokens'
export const DEFAULT_LIMIT = 20

export interface AnalyticsFilters {
  from?: string
  to?: string
  bucket?: Bucket
  project?: string
  group?: GroupBy
  measure?: Measure
  model?: string[]
  stage?: string[]
  agent?: string[]
  test?: string
  status?: 'ok' | 'error'
  sort?: AgentSort
  dir?: AgentOrder
  offset?: number
}

export function isBucket(value: unknown): value is Bucket {
  return typeof value === 'string' && (BUCKETS as readonly string[]).includes(value)
}

export function isGroupBy(value: unknown): value is GroupBy {
  return typeof value === 'string' && (GROUP_BYS as readonly string[]).includes(value)
}

export function isMeasure(value: unknown): value is Measure {
  return typeof value === 'string' && (MEASURES as readonly string[]).includes(value)
}

export function isStatus(value: unknown): value is 'ok' | 'error' {
  return value === 'ok' || value === 'error'
}

const SORTS = [
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
] as const satisfies readonly AgentSort[]

export function isSort(value: unknown): value is AgentSort {
  return typeof value === 'string' && (SORTS as readonly string[]).includes(value)
}

export function isOrder(value: unknown): value is AgentOrder {
  return value === 'asc' || value === 'desc'
}

function parseIso(raw: string | null): string | undefined {
  if (!raw) return undefined
  return Number.isNaN(Date.parse(raw)) ? undefined : raw
}

function parseIntParam(raw: string | null): number | undefined {
  if (!raw) return undefined
  const value = Number(raw)
  return Number.isInteger(value) && value >= 0 ? value : undefined
}

export function parseCsv(raw: string | null): string[] | undefined {
  if (!raw) return undefined
  const values = raw
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)
  return values.length ? values : undefined
}

function writeCsv(params: URLSearchParams, key: string, values: string[] | undefined): void {
  // Mirror parseCsv: drop blanks so a `['']` array never serializes an empty `key=`.
  const clean = values?.map((value) => value.trim()).filter(Boolean)
  if (clean?.length) params.set(key, clean.join(','))
}

export function parseAnalyticsFilters(params: URLSearchParams): AnalyticsFilters {
  const bucket = params.get('bucket')
  const group = params.get('group')
  const measure = params.get('measure')
  const status = params.get('status')
  const sort = params.get('sort')
  const dir = params.get('dir')
  const project = params.get('project')?.trim()
  const test = params.get('test')?.trim()
  return {
    ...(parseIso(params.get('from')) ? { from: parseIso(params.get('from')) } : {}),
    ...(parseIso(params.get('to')) ? { to: parseIso(params.get('to')) } : {}),
    ...(isBucket(bucket) ? { bucket } : {}),
    ...(project ? { project } : {}),
    ...(isGroupBy(group) ? { group } : {}),
    ...(isMeasure(measure) ? { measure } : {}),
    ...(parseCsv(params.get('model')) ? { model: parseCsv(params.get('model')) } : {}),
    ...(parseCsv(params.get('stage')) ? { stage: parseCsv(params.get('stage')) } : {}),
    ...(parseCsv(params.get('agent')) ? { agent: parseCsv(params.get('agent')) } : {}),
    ...(test ? { test } : {}),
    ...(isStatus(status) ? { status } : {}),
    ...(isSort(sort) ? { sort } : {}),
    ...(isOrder(dir) ? { dir } : {}),
    ...(parseIntParam(params.get('offset')) ? { offset: parseIntParam(params.get('offset')) } : {}),
  }
}

export function serializeAnalyticsFilters(filters: AnalyticsFilters): URLSearchParams {
  const params = new URLSearchParams()
  if (filters.from) params.set('from', filters.from)
  if (filters.to) params.set('to', filters.to)
  if (filters.bucket) params.set('bucket', filters.bucket)
  if (filters.project) params.set('project', filters.project)
  if (filters.group && filters.group !== DEFAULT_GROUP) params.set('group', filters.group)
  if (filters.measure && filters.measure !== DEFAULT_MEASURE) {
    params.set('measure', filters.measure)
  }
  writeCsv(params, 'model', filters.model)
  writeCsv(params, 'stage', filters.stage)
  writeCsv(params, 'agent', filters.agent)
  if (filters.test) params.set('test', filters.test)
  if (filters.status) params.set('status', filters.status)
  if (filters.sort) params.set('sort', filters.sort)
  if (filters.dir) params.set('dir', filters.dir)
  if (filters.offset) params.set('offset', String(filters.offset))
  return params
}

export const AUTO_HOUR_MAX_MS = 48 * HOUR_MS
const DEFAULT_SPAN_MS = 7 * DAY_MS

export function effectiveBucket(filters: AnalyticsFilters, now: number = Date.now()): Bucket {
  if (filters.bucket) return filters.bucket
  const to = filters.to ? Date.parse(filters.to) : now
  const from = filters.from ? Date.parse(filters.from) : to - DEFAULT_SPAN_MS
  return to - from <= AUTO_HOUR_MAX_MS ? 'hour' : 'day'
}

export function defaultSortFor(measure: Measure): AgentSort {
  if (measure === 'cost') return 'cost_usd'
  if (measure === 'latency') return 'p95_latency_ms'
  return 'total_tokens'
}

export function hasAnalyticsFilters(filters: AnalyticsFilters): boolean {
  return Boolean(
    filters.from ||
      filters.to ||
      filters.bucket ||
      filters.project ||
      filters.group ||
      filters.measure ||
      filters.model?.length ||
      filters.stage?.length ||
      filters.agent?.length ||
      filters.test ||
      filters.status ||
      filters.sort ||
      filters.dir ||
      filters.offset,
  )
}
