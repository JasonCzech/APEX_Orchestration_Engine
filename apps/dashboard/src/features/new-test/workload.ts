/** Mirrors LoadTestSpec and the script_scenario defaults. These are requested
 * execution inputs, never measurements inferred from the request prose. */
export const WORKLOAD_FIELDS = [
  { key: 'vusers', label: 'VUsers', min: 1, max: 10_000, step: 1, defaultValue: 10 },
  { key: 'ramp_s', label: 'Ramp-up', min: 0, max: 86_400, step: 'any', defaultValue: 1 },
  { key: 'duration_s', label: 'Duration', min: 0, max: 86_400, step: 'any', defaultValue: 2 },
] as const

export const SLA_FIELDS = [
  { key: 'p95_ms', label: 'Maximum P95 (ms)', max: 1e12, scale: 1 },
  { key: 'error_rate', label: 'Maximum error rate (%)', max: 100, scale: 100 },
  { key: 'tps_avg', label: 'Minimum throughput (TPS)', max: 1e12, scale: 1 },
] as const

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

export function effectiveWorkload(value: unknown): Record<string, unknown> & { slas: Record<string, unknown> } {
  const configured = record(value)
  return {
    ...Object.fromEntries(WORKLOAD_FIELDS.map(field => [field.key, field.defaultValue])),
    ...configured,
    slas: configured.slas === undefined ? { p95_ms: 500, error_rate: 0.05 } : record(configured.slas),
  }
}

export function inputNumber(value: unknown, scale = 1): string | number {
  return typeof value === 'number' && Number.isFinite(value) ? value * scale : typeof value === 'string' ? value : ''
}

export function workloadIssues(value: unknown): string[] {
  const effective = effectiveWorkload(value)
  const issues: string[] = []
  if (value !== undefined && (value === null || typeof value !== 'object' || Array.isArray(value))) {
    issues.push('Workload must be a valid object; reset workload defaults or choose a valid preset')
  }
  for (const field of WORKLOAD_FIELDS) {
    const number = effective[field.key]
    if (typeof number !== 'number' || !Number.isFinite(number) || number < field.min || number > field.max ||
      (field.key === 'vusers' && !Number.isInteger(number)) || (field.key === 'duration_s' && number === 0)) {
      const range = field.key === 'vusers' ? `a whole number between ${field.min} and ${field.max}`
        : field.key === 'duration_s' ? 'greater than 0 and no more than 24 hours' : 'between 0 and 24 hours'
      issues.push(`${field.label} must be ${range}`)
    }
  }
  // A ramp longer than the test never reaches the requested load.
  if (typeof effective.ramp_s === 'number' && typeof effective.duration_s === 'number' &&
    Number.isFinite(effective.ramp_s) && Number.isFinite(effective.duration_s) && effective.ramp_s > effective.duration_s) {
    issues.push('Ramp-up must not be longer than the duration')
  }
  const configured = record(value)
  if (Object.keys(configured).length > 16) issues.push('Workload configuration cannot contain more than 16 fields')
  if (configured.slas !== undefined && (configured.slas === null || typeof configured.slas !== 'object' || Array.isArray(configured.slas))) {
    issues.push('Acceptance thresholds must be a valid object; choose a valid preset or clear it')
  }
  if (Object.keys(effective.slas).length > 32) issues.push('Acceptance thresholds cannot contain more than 32 entries')
  for (const [key, number] of Object.entries(effective.slas)) {
    const field = SLA_FIELDS.find(field => field.key === key)
    if (!field) issues.push(`Acceptance threshold ${key} is not supported by this wizard; reset workload defaults or choose a compatible preset`)
    const max = key === 'error_rate' ? 1 : 1e12
    if (!key.trim() || key.length > 64 || key.includes('\0') || typeof number !== 'number' || !Number.isFinite(number) || number < 0 || number > max) {
      issues.push(`${field?.label ?? key} must be between 0 and ${field?.max ?? max}`)
    }
  }
  return issues
}

export const DURATION_UNITS = [
  { label: 'seconds', seconds: 1 },
  { label: 'minutes', seconds: 60 },
  { label: 'hours', seconds: 3600 },
] as const

/** The largest unit that shows a stored seconds value as a whole number. */
export function durationUnit(seconds: unknown): number {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds <= 0) return 1
  return [...DURATION_UNITS].reverse().find(unit => seconds % unit.seconds === 0)?.seconds ?? 1
}
