import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { KpiComparison } from '../KpiComparison'

describe('KpiComparison', () => {
  it('judges each observed KPI against its applied threshold in the right direction', () => {
    render(<KpiComparison
      summary={{ engine: 'sim', passed: false, kpis: { p95_ms: 212, error_rate: 0.004, tps_avg: 8, vusers_peak: 10 } }}
      thresholds={{ tps_avg: 10, p95_ms: 500, error_rate: 0.05 }}
    />)
    const row = (label: string) => within(screen.getByRole('row', { name: new RegExp(`^${label}`) }))
    expect(row('P95 response time').getByText('≤ 500 ms')).toBeInTheDocument()
    expect(row('P95 response time').getByText('Met')).toBeInTheDocument()
    expect(row('Error rate').getByText('≤ 5 %')).toBeInTheDocument()
    expect(row('Error rate').getByText('0.4 %')).toBeInTheDocument()
    expect(row('Average throughput').getByText('≥ 10 TPS')).toBeInTheDocument()
    expect(row('Average throughput').getByText('Not met')).toBeInTheDocument()
    expect(row('Peak VUsers').getByText('None set')).toBeInTheDocument()
    expect(screen.getAllByRole('row').slice(1).map(r => within(r).getByRole('rowheader').textContent)).toEqual(
      ['P95 response time', 'Error rate', 'Average throughput', 'Peak VUsers'],
    )
    expect(screen.getByText('Simulated engine — KPIs are synthetic.')).toBeInTheDocument()
  })

  it('shows a threshold whose KPI was not reported without claiming a result', () => {
    render(<KpiComparison summary={{ engine: 'sim', kpis: { tps_avg: 4 } }} thresholds={{ p95_ms: 300 }} />)
    const row = within(screen.getByRole('row', { name: /^P95 response time/ }))
    expect(row.getByText('Not reported')).toBeInTheDocument()
    expect(row.getByText('Not evaluated')).toBeInTheDocument()
  })

  it('defers to a real engine\'s verdict instead of judging its aggregates per row', () => {
    render(<KpiComparison summary={{ engine: 'apex_load', passed: false, kpis: { p95_ms: 280 } }} thresholds={{ p95_ms: 300 }} />)
    expect(screen.getByText(/Engine verdict: Failed\./)).toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: 'Result' })).not.toBeInTheDocument()
    expect(screen.queryByText('Met')).not.toBeInTheDocument()
    expect(screen.getByText(/APEX Load assessed these thresholds/)).toBeInTheDocument()
  })

  it('flags a simulated failure the thresholds alone do not explain', () => {
    render(<KpiComparison summary={{ engine: 'sim', passed: false, kpis: { p95_ms: 200 }, sla_breaches: ['run failed before completion (injected failure)'] }} thresholds={{ p95_ms: 500 }} />)
    expect(screen.getByText(/engine reported a failure: run failed before completion/)).toBeInTheDocument()
  })

  it('renders nothing without KPIs or thresholds', () => {
    const { container } = render(<KpiComparison summary={{ engine: 'sim' }} />)
    expect(container).toBeEmptyDOMElement()
  })
})
