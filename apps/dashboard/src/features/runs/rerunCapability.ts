/** Capability comes from the scoped server projection, never inferred from phase state. */
export function rerunRestriction(reason: string | null | undefined): string | null {
  return reason === 'cycle_preparation_required'
    ? 'Cycle executions require a new approved preparation before another launch.'
    : null
}
