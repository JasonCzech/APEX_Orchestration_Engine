import { useState } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { HumanCompletionPayload } from '@apex/pipeline-events'
import { HumanCompletionPanel } from '../HumanCompletionPanel'

function Harness({ phase = 'reporting' }: { phase?: 'reporting' | 'execution' | 'test_planning' }) {
  const [output, setOutput] = useState<Record<string, unknown>>()
  const payload = { schema_version: 1, kind: 'human_completion', phase, task_id: 'task', revision: 1, actions: ['complete', 'abort'], sections: [], artifact_ids: [], engine: 'apex_load', created_at: '2026-10-22T00:00:00Z', requires_load_test_spec: false, approved_target: null, analysis_selections: phase === 'reporting' ? [{ tool: 'perfreportforge', connection_id: 'forge' }] : [] } as HumanCompletionPayload
  return <><HumanCompletionPanel payload={payload} output={output} disabled={false} onChange={setOutput} /><pre data-testid="output">{JSON.stringify(output)}</pre></>
}
const output = () => JSON.parse(screen.getByTestId('output').textContent || '{}')

describe('human completion form', () => {
  it('preserves manually authored evidence while tool URLs are edited', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await user.type(screen.getByLabelText(/Additional evidence/), 'https://evidence.test/manual\nhttps://evidence.test/second')
    const refs = screen.getByLabelText(/perfreportforge result references/)
    await user.type(refs, 'https://reports.test/first\nhttps://reports.test/second')
    await user.clear(refs)
    await user.type(refs, 'https://reports.test/final')
    expect(output().deliverable.evidence).toEqual(['https://evidence.test/manual', 'https://evidence.test/second', 'https://reports.test/final'])
    expect(output().analysis_results.forge).toEqual(['https://reports.test/final'])
  })
  it('keeps decimal input editable and removes cleared measurements', async () => {
    const user = userEvent.setup()
    render(<Harness phase="execution" />)
    const kpi = screen.getByLabelText('error_rate')
    await user.type(kpi, '0.05')
    expect(kpi).toHaveValue('0.05')
    expect(output().external_results.kpis.error_rate).toBe(0.05)
    await user.clear(kpi)
    expect(output().external_results.kpis).not.toHaveProperty('error_rate')
  })
  it('retains multiline workload editing across derived output updates', async () => {
    const user = userEvent.setup()
    render(<Harness phase="test_planning" />)
    await user.type(screen.getByLabelText(/Workload/), 'checkout,70\nsearch,30')
    expect(output().deliverable.workload).toEqual([{ scenario_id: 'checkout', percent: 70 }, { scenario_id: 'search', percent: 30 }])
  })
})
