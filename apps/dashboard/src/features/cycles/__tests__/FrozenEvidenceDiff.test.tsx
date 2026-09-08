import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { FrozenEvidenceDiff } from '../FrozenEvidenceDiff'

const row = {
  id: 'run',
  engine: 'apex-load',
  attempt: 1,
  execution_status: 'completed',
  test_outcome: null,
  metrics: [
    { key: 'error_rate', label: 'Error rate', unit: 'ratio', value: null },
  ],
  sla_breaches: [],
  warnings: [],
  bindings_verified: false,
}
describe('Frozen report evidence comparison', () => {
  it('does not describe recapture metadata as changed evidence', () => {
    render(
      <FrozenEvidenceDiff
        baseline={{
          captured_at: 'yesterday',
          results: [{ ...row, evidence_checksum: 'one' }],
        }}
        selected={{
          captured_at: 'today',
          results: [{ ...row, evidence_checksum: 'two' }],
        }}
      />,
    )
    expect(
      screen.getByText('Frozen execution evidence is unchanged.'),
    ).toBeInTheDocument()
  })
  it('shows unknown to failed and missing to measured zero explicitly', () => {
    render(
      <FrozenEvidenceDiff
        baseline={{ results: [row] }}
        selected={{
          results: [
            {
              ...row,
              test_outcome: 'failed',
              metrics: [{ ...row.metrics[0], value: 0 }],
              sla_breaches: ['Latency exceeded threshold'],
            },
          ],
        }}
      />,
    )
    expect(
      screen.getByText('Test outcome: Not available → Failed'),
    ).toBeInTheDocument()
    expect(screen.getByRole('definition')).toHaveTextContent(
      'Not available ratio → 0 ratio',
    )
    expect(
      screen.getByText('Selected: Latency exceeded threshold'),
    ).toBeInTheDocument()
    expect(
      screen.getByText('0 attempts added · 0 removed · 1 changed'),
    ).toBeInTheDocument()
  })
  it('identifies added and removed attempts rather than comparing unrelated measurements', () => {
    render(
      <FrozenEvidenceDiff
        baseline={{ results: [row] }}
        selected={{ results: [{ ...row, id: 'new-run', attempt: 2 }] }}
      />,
    )
    expect(
      screen.getByText('1 attempts added · 1 removed · 0 changed'),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { name: 'Removed attempts' }),
    ).toBeInTheDocument()
  })
  it('does not treat missing or malformed historical snapshots as unchanged', () => {
    render(
      <FrozenEvidenceDiff baseline={{ results: [null] }} selected={null} />,
    )
    expect(
      screen.getByText(/comparison version has no readable frozen evidence/),
    ).toBeInTheDocument()
    expect(
      screen.queryByText('Frozen execution evidence is unchanged.'),
    ).not.toBeInTheDocument()
  })
})

it('does not call unreadable evidence a removed attempt', () => {
  render(<FrozenEvidenceDiff baseline={{ results: [row] }} selected={null} />)
  expect(
    screen.queryByRole('heading', { name: 'Removed attempts' }),
  ).not.toBeInTheDocument()
})

it('ignores metric ordering when values are unchanged', () => {
  const another = {
    key: 'p95_ms',
    label: 'Response time',
    unit: 'ms',
    value: 100,
  }
  render(
    <FrozenEvidenceDiff
      baseline={{ results: [{ ...row, metrics: [row.metrics[0], another] }] }}
      selected={{ results: [{ ...row, metrics: [another, row.metrics[0]] }] }}
    />,
  )
  expect(
    screen.getByText('Frozen execution evidence is unchanged.'),
  ).toBeInTheDocument()
})
