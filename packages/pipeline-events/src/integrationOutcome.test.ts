import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { IntegrationOutcomeSchema, TestResultSummarySchema } from './state';

const fixture = IntegrationOutcomeSchema.parse(JSON.parse(readFileSync(new URL('../../../docs/contracts/apex-load-v1/outcome.not-assessed.json', import.meta.url), 'utf8')));

describe('shared Load outcome contract', () => {
  it('accepts the shared fixture and older summaries without a contract', () => {
    expect(IntegrationOutcomeSchema.parse(fixture).assessment.status).toBe('not_assessed');
    expect(TestResultSummarySchema.parse({ engine: 'apex_load', passed: null }).outcome).toBeUndefined();
  });
  it('rejects unknown versions and non-finite durations', () => {
    expect(IntegrationOutcomeSchema.safeParse({ ...fixture, schema_version: 2 }).success).toBe(false);
    expect(IntegrationOutcomeSchema.safeParse({ ...fixture, execution: { ...fixture.execution, planned_duration_seconds: Infinity } }).success).toBe(false);
  });
});
