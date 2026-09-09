import { describe, expect, it } from 'vitest'
import { executionLink, safeExecutionUrl } from './executionLink'
describe('APEX execution links', () => {
  it('binds the return context to the exact Load execution', () => {
    const link = new URL(
      executionLink('https://load.example/#/test/run-1', '/apex-load?connection=c1&run=run-1')!,
    )
    expect(link.hash).toBe('#/test/run-1')
    expect(link.searchParams.get('orchestrationRun')).toBe('run-1')
    expect(new URL(link.searchParams.get('orchestrationReturn')!).pathname).toBe('/apex-load')
  })
  it.each([
    'https://load.example/#/test/%',
    'https://load.example/#/test/%FF',
    'https://load.example/#/test/a%2Fb',
    'javascript:alert(1)',
    'https://user:secret@load.example/#/test/run-1',
    'https://load.example/?token=secret#/test/run-1',
    'https://load.example/#/settings',
    'https://load.example/#/test/run-1?redirect=evil',
  ])('rejects unsafe or unrelated execution link %s', (value) => {
    expect(safeExecutionUrl(value)).toBeNull()
    expect(executionLink(value, '/runs/run-1')).toBeNull()
  })
})

it('links directly to an evidence section with the same execution return binding', () => {
  const link = new URL(executionLink('https://load.example/#/test/run-1', '/runs/orch-1', 'evidence')!)
  expect(link.hash).toBe('#/report/run-1?section=evidence')
  expect(link.searchParams.get('orchestrationRun')).toBe('run-1')
  expect(safeExecutionUrl('https://load.example/#/report/run-1?section=diagnostics')).not.toBeNull()
  expect(safeExecutionUrl('https://load.example/#/report/run-1?section=evidence&redirect=evil')).toBeNull()
})

it.each(['tenant:run-1', 'tenant%3Arun-1'])('preserves provider namespaced execution IDs: %s', id => {
  const link = new URL(executionLink(`https://load.example/#/test/${id}`, '/runs/orch-1', 'diagnostics')!)
  expect(link.hash).toBe(`#/report/${id}?section=diagnostics`)
  expect(link.searchParams.get('orchestrationRun')).toBe('tenant:run-1')
})
