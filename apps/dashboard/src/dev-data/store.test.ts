import { beforeEach, describe, expect, it } from 'vitest'

import type { AgentAnalytics } from '@/api/hooks/useAgentAnalytics'
import { parseGateInterrupt, parsePipelineEvent } from '@apex/pipeline-events'

import { createDevDataStore, DEMO_STATE_STORAGE_KEY } from './store'
import { deterministicAgentReply, FLAGSHIP_ALIGN_QUERY, TELECOM_DATASET_REQUIREMENTS } from './telecomFixtures'

const WIDE_WINDOW = 'from=2000-01-01T00:00:00.000Z&to=2100-01-01T00:00:00.000Z'

async function jsonOf<T>(response: Response | null): Promise<T> {
  expect(response).not.toBeNull()
  return (await (response as Response).json()) as T
}

describe('dev-data store', () => {
  beforeEach(() => localStorage.clear())

  it('derives every demo summary from its corresponding phase state', async () => {
    const store = createDevDataStore()
    const list = await jsonOf<{ items: Array<import('@/api/hooks/usePipelines').PipelineSummary> }>(
      await store.handleApexRequest(new Request('http://localhost/v1/pipelines?limit=100')),
    )
    for (const summary of list.items) {
      const detail = await jsonOf<import('@/api/hooks/useThreadState').PipelineDetail>(
        await store.handleApexRequest(new Request(`http://localhost/v1/pipelines/${summary.thread_id}`)),
      )
      const state = detail.values as import('@apex/pipeline-events').PipelineState
      expect(summary.phase_strip).toEqual(detail.phase_strip)
      for (const phase of summary.phase_strip) {
        expect(phase.status).toBe(state.phases_plan && !state.phases_plan.includes(phase.phase as import('@apex/pipeline-events').PhaseName)
          ? 'none' : state.phase_results?.[phase.phase as import('@apex/pipeline-events').PhaseName]?.status ?? 'pending')
      }
      const passed = state.phase_results?.execution?.test_summary?.passed
      expect(summary.execution_result).toBe(passed === true ? 'passed' : passed === false ? 'failed' : null)
    }
    expect(list.items.find((run) => run.thread_id === 'run-conditional')?.execution_result).toBe('failed')
  })

  it('filters and paginates pipeline summaries', async () => {
    const store = createDevDataStore()

    const body = await jsonOf<{ items: Array<{ thread_status: string; pending_gate?: unknown }>; total: number }>(
      await store.handleApexRequest(
        new Request('http://localhost/v1/pipelines?status=interrupted&limit=1&offset=0'),
      ),
    )

    expect(body.total).toBeGreaterThanOrEqual(2)
    expect(body.items).toHaveLength(1)
    expect(body.items[0]?.thread_status).toBe('interrupted')
    expect(body.items[0]?.pending_gate).toBeTruthy()
  })

  it('keeps create/delete mutations inside the in-memory session', async () => {
    const store = createDevDataStore()
    const createResponse = await store.handleApexRequest(
      new Request('http://localhost/v1/work-tracking/saved-queries', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          name: 'Fresh dummy query',
          provider: 'jira',
          query: 'project = PHX',
          project_id: 'proj-alpha',
        }),
      }),
    )
    const created = await jsonOf<{ id: string; name: string }>(createResponse)

    const list = await jsonOf<{ items: Array<{ id: string; name: string }> }>(
      await store.handleApexRequest(new Request('http://localhost/v1/work-tracking/saved-queries')),
    )
    expect(list.items.some((item) => item.id === created.id && item.name === 'Fresh dummy query')).toBe(
      true,
    )

    const deleteResponse = await store.handleApexRequest(
      new Request(`http://localhost/v1/work-tracking/saved-queries/${created.id}`, {
        method: 'DELETE',
      }),
    )
    expect(deleteResponse?.status).toBe(204)
  })

  it('serves local artifact bytes', async () => {
    const store = createDevDataStore()

    const bytes = store.getArtifactBytes('http://localhost/v1/artifacts/reports/exec-report')

    expect(bytes?.mediaType).toBe('application/json')
    expect(await bytes?.blob.text()).toContain('apexload')
  })

  it('returns an explicit offline-demo problem for unhandled v1 routes', async () => {
    const store = createDevDataStore()

    const response = await store.handleApexRequest(new Request('http://localhost/v1/nope'))
    const body = await jsonOf<{ title: string }>(response)

    expect(response?.status).toBe(501)
    expect(body.title).toBe('demo_handler_missing')
  })

  it('seeds the complete provider-bound telecom catalog', async () => {
    const store = createDevDataStore()
    expect(store.getFixtureStats()).toMatchObject(TELECOM_DATASET_REQUIREMENTS)
    const environment = await jsonOf<{ name: string; options: { dependencies: Array<{ application: string; environment: string }> } }>(
      await store.handleApexRequest(new Request('http://localhost/v1/catalog/environments/env-perf')),
    )
    expect(environment.name).toBe('PERFHYPER2')
    expect(environment.options.dependencies).toEqual(expect.arrayContaining([
      expect.objectContaining({ application: 'Mulesoft', environment: 'TEST' }),
      expect.objectContaining({ application: 'BSSe', environment: 'TEST' }),
    ]))
    const align = await jsonOf<{ items: Array<{ key: string }>; provider: string }>(
      await store.handleApexRequest(new Request('http://localhost/v1/work-tracking/query/execute', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ connection_id: 'conn-align', query: { provider: 'jira_align', query: "programId eq 42 and contains(title,'ACC add-a-line')" } }) })),
    )
    const jira = await jsonOf<{ items: Array<{ key: string }>; provider: string }>(
      await store.handleApexRequest(new Request('http://localhost/v1/work-tracking/query/execute', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ connection_id: 'conn-jira', query: { provider: 'jira', query: 'project = PERF' } }) })),
    )
    const prompts = await jsonOf<unknown[]>(await store.handleApexRequest(new Request('http://localhost/v1/prompts?include_archived=true&limit=50')))

    expect(align.provider).toBe('jira_align')
    expect(align.items.map((item) => item.key)).toEqual(expect.arrayContaining(['feature:583', 'story:9012']))
    expect(align.items.every((item) => item.key.includes(':'))).toBe(true)
    expect(jira.provider).toBe('jira')
    expect(jira.items).toHaveLength(TELECOM_DATASET_REQUIREMENTS.initialJiraCloudItems)
    expect(jira.items.every((item) => item.key.startsWith('PERF-'))).toBe(true)
    expect(prompts).toHaveLength(TELECOM_DATASET_REQUIREMENTS.prompts)
  })

  it('translates Jira Align natural language and evaluates saved Align item types', async () => {
    const store = createDevDataStore()
    const translated = await jsonOf<{ provider: string; connection_id: string; query: string }>(
      await store.handleApexRequest(new Request('http://localhost/v1/work-tracking/query/translate', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ text: 'Find Jira Align ACC add-a-line release scope', connection_id: 'conn-jira' }),
      })),
    )
    expect(translated).toMatchObject({ provider: 'jira_align', connection_id: 'conn-align', query: FLAGSHIP_ALIGN_QUERY })

    const risks = await jsonOf<{ items: Array<{ key: string; kind: string }> }>(
      await store.handleApexRequest(new Request('http://localhost/v1/work-tracking/query/execute', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ connection_id: 'conn-align', query: { provider: 'jira_align', query: "programId eq 42 and itemtype in ('bug') and state eq 'Open'" } }),
      })),
    )
    expect(risks.items.map((item) => item.key)).toEqual(['bug:9441', 'bug:9460'])
    expect(risks.items.every((item) => item.kind === 'bug')).toBe(true)
  })

  it('uses phase-safe deterministic response branches without invoking an LLM', () => {
    expect(deterministicAgentReply('env_triage', 'Why is the MQ queue growing?')).toContain('four consumers')
    expect(deterministicAgentReply('reporting', 'What is the SLA release risk?')).toContain('conditional certification')
    expect(deterministicAgentReply('env_triage', 'Is the Oracle configuration correct?')).toContain('non-PERFHYPER2 BSSe test endpoint')
  })

  it('reserves PERF-248 for the scripted finding and sequences manual Jira Cloud tickets from PERF-249', async () => {
    const store = createDevDataStore()
    const created = await jsonOf<{ key: string; provider: string }>(
      await store.handleApexRequest(new Request('http://localhost/v1/work-tracking/items?connection_id=conn-jira', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ title: 'Manual follow-up', kind: 'task' }) })),
    )
    expect(created).toMatchObject({ key: 'PERF-249', provider: 'jira' })
  })

  it('launches the production wizard entrypoint idempotently with a fresh phase snapshot', async () => {
    const store = createDevDataStore()
    const request = () => new Request('http://localhost/v1/pipelines', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ idempotency_key: 'wizard-flagship', title: 'ACC Add-a-Line Peak Certification', request: 'Certify the flagship.', project_id: 'telecom-modernization', app_id: 'app-digital-device' }),
    })
    const launched = await jsonOf<{ thread_id: string; run_id: string }>(await store.handleApexRequest(request()))
    const replay = await jsonOf<{ thread_id: string; run_id: string }>(await store.handleApexRequest(request()))
    expect(replay).toEqual(launched)
    const detail = await jsonOf<{ current_phase: string; phase_strip: Array<{ phase: string; status: string }> }>(await store.handleApexRequest(new Request(`http://localhost/v1/pipelines/${launched.thread_id}`)))
    expect(detail.current_phase).toBe('story_analysis')
    expect(detail.phase_strip.find((phase) => phase.phase === 'story_analysis')?.status).toBe('running')
    expect(detail.phase_strip.filter((phase) => phase.phase !== 'story_analysis').every((phase) => phase.status === 'pending')).toBe(true)
  })

  it('hydrates compatible journals and safely discards corrupt state', async () => {
    const store = createDevDataStore()
    await store.handleApexRequest(new Request('http://localhost/v1/work-tracking/saved-queries', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ name: 'Persisted query', provider: 'jira', query: 'project = PERF', project_id: 'telecom-modernization' }) }))
    expect(localStorage.getItem(DEMO_STATE_STORAGE_KEY)).toContain('Persisted query')
    const hydrated = createDevDataStore()
    const list = await jsonOf<{ items: Array<{ name: string }> }>(await hydrated.handleApexRequest(new Request('http://localhost/v1/work-tracking/saved-queries')))
    expect(list.items.some((item) => item.name === 'Persisted query')).toBe(true)

    localStorage.setItem(DEMO_STATE_STORAGE_KEY, '{bad json')
    const recovered = createDevDataStore()
    expect(localStorage.getItem(DEMO_STATE_STORAGE_KEY)).toBeNull()
    const canonical = await jsonOf<{ items: unknown[] }>(await recovered.handleApexRequest(new Request('http://localhost/v1/work-tracking/saved-queries')))
    expect(canonical.items).toHaveLength(4)

    localStorage.setItem(DEMO_STATE_STORAGE_KEY, JSON.stringify({
      version: 3,
      activationTime: new Date().toISOString(),
      nextId: 101,
      mutations: { workItemProviders: { upsert: 'not-an-array', removed: [] } },
      completedRunStreams: [],
    }))
    const structurallyRecovered = createDevDataStore()
    expect(localStorage.getItem(DEMO_STATE_STORAGE_KEY)).toBeNull()
    const align = await jsonOf<{ items: Array<{ key: string }> }>(await structurallyRecovered.handleApexRequest(new Request('http://localhost/v1/work-tracking/query/execute', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ connection_id: 'conn-align', query: { provider: 'jira_align', query: FLAGSHIP_ALIGN_QUERY } }) })))
    expect(align.items).toHaveLength(4)
  })

  it('resumes streams after the committed event id without replaying applied events', async () => {
    const store = createDevDataStore()
    const thread = store.createThread()
    const run = store.createRun(thread.thread_id, 'pipeline', { input: { title: 'Cursor recovery' } })
    const stream = store.joinRunStream(thread.thread_id, run.run_id)
    const first = await stream.next()
    const second = await stream.next()
    await stream.return(undefined)
    expect(first.value?.id).toBe(`${run.run_id}-plan`)
    expect(second.value?.id).toBe(`${run.run_id}-story_analysis-start`)

    const hydrated = createDevDataStore()
    const resumed = hydrated.joinRunStream(thread.thread_id, run.run_id, { lastEventId: second.value?.id })
    const next = await resumed.next()
    expect(next.value?.id).toBe(`${run.run_id}-story_analysis-agent`)
    expect(next.value?.id).not.toBe(first.value?.id)
    await resumed.return(undefined)
  })

  it('reruns a checkpointed phase subset idempotently and persists the completed snapshot', async () => {
    const store = createDevDataStore()
    const request = () => new Request('http://localhost/v1/pipelines/run-baseline-a/rerun', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        phases: ['execution', 'reporting'],
        gates_mode: 'auto',
        idempotency_key: 'rerun-baseline-a',
      }),
    })
    const first = await jsonOf<{ run_id: string }>(await store.handleApexRequest(request()))
    const replay = await jsonOf<{ run_id: string }>(await store.handleApexRequest(request()))
    expect(replay.run_id).toBe(first.run_id)

    const events: Array<{ data: unknown }> = []
    for await (const event of store.joinRunStream('run-baseline-a', first.run_id)) {
      parsePipelineEvent(event.data)
      events.push(event)
    }
    expect(JSON.stringify(events)).toContain('execution')
    expect(JSON.stringify(events)).toContain('reporting')
    expect(JSON.stringify(events)).not.toContain('story_analysis-start')

    const hydrated = createDevDataStore()
    const detail = await jsonOf<{
      thread_status: string
      phase_strip: Array<{ phase: string; status: string; attempt?: number }>
    }>(await hydrated.handleApexRequest(new Request('http://localhost/v1/pipelines/run-baseline-a')))
    expect(detail.thread_status).toBe('idle')
    expect(detail.phase_strip.find((item) => item.phase === 'execution')).toMatchObject({
      status: 'succeeded',
      attempt: 2,
    })
  })

  it('reopens and resolves a gated subset rerun without leaving the flagship state machine', async () => {
    const store = createDevDataStore()
    const started = await jsonOf<{ run_id: string }>(await store.handleApexRequest(new Request(
      'http://localhost/v1/pipelines/run-baseline-b/rerun',
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          phases: ['story_analysis'],
          gates_mode: 'gated',
          idempotency_key: 'rerun-baseline-b',
        }),
      },
    )))
    for await (const event of store.joinRunStream('run-baseline-b', started.run_id)) {
      parsePipelineEvent(event.data)
    }
    let detail = await jsonOf<{
      thread_status: string
      pending_gate: { interrupt_id: string }
      values: { dialogue: Array<{ content: string }> }
    }>(await store.handleApexRequest(new Request('http://localhost/v1/pipelines/run-baseline-b')))
    expect(detail.thread_status).toBe('interrupted')

    const discussed = await jsonOf<{ run_id: string }>(await store.handleApexRequest(new Request(
      `http://localhost/v1/pipelines/run-baseline-b/gates/${detail.pending_gate.interrupt_id}/resume`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ action: 'discuss', message: 'What is the release SLA risk?' }),
      },
    )))
    for await (const event of store.joinRunStream('run-baseline-b', discussed.run_id)) {
      parsePipelineEvent(event.data)
    }
    detail = await jsonOf(await store.handleApexRequest(new Request('http://localhost/v1/pipelines/run-baseline-b')))
    expect(detail.values.dialogue.some((entry) => entry.content === 'What is the release SLA risk?')).toBe(true)

    const approved = await jsonOf<{ run_id: string }>(await store.handleApexRequest(new Request(
      `http://localhost/v1/pipelines/run-baseline-b/gates/${detail.pending_gate.interrupt_id}/resume`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ action: 'approve' }),
      },
    )))
    for await (const event of store.joinRunStream('run-baseline-b', approved.run_id)) {
      parsePipelineEvent(event.data)
    }
    const completed = await jsonOf<{ thread_status: string; pending_gate: unknown }>(
      await store.handleApexRequest(new Request('http://localhost/v1/pipelines/run-baseline-b')),
    )
    expect(completed).toMatchObject({ thread_status: 'idle', pending_gate: null })
  })

  it('applies the project filter to context evidence packets', async () => {
    const store = createDevDataStore()
    const matching = await jsonOf<unknown[]>(await store.handleApexRequest(new Request(
      'http://localhost/v1/context/evidence?project=telecom-modernization',
    )))
    const unrelated = await jsonOf<unknown[]>(await store.handleApexRequest(new Request(
      'http://localhost/v1/context/evidence?project=another-project',
    )))
    expect(matching).toHaveLength(TELECOM_DATASET_REQUIREMENTS.evidencePackets)
    expect(unrelated).toEqual([])
  })

  it('keeps abort terminal and retries engine provisioning through reconciliation', async () => {
    const store = createDevDataStore()
    const thread = store.createThread()
    const initial = store.createRun(thread.thread_id, 'pipeline', { input: { title: 'Abort rehearsal' } })
    for await (const _event of store.joinRunStream(thread.thread_id, initial.run_id)) void _event
    let detail = await jsonOf<{ pending_gate: { interrupt_id: string } }>(await store.handleApexRequest(new Request(`http://localhost/v1/pipelines/${thread.thread_id}`)))
    const aborted = await jsonOf<{ run_id: string }>(await store.handleApexRequest(new Request(`http://localhost/v1/pipelines/${thread.thread_id}/gates/${detail.pending_gate.interrupt_id}/resume`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ action: 'abort' }) })))
    for await (const _event of store.joinRunStream(thread.thread_id, aborted.run_id)) void _event
    const abortedDetail = await jsonOf<{ thread_status: string; values: { run_aborted: boolean } }>(await store.handleApexRequest(new Request(`http://localhost/v1/pipelines/${thread.thread_id}`)))
    expect(abortedDetail).toMatchObject({ thread_status: 'error', values: { run_aborted: true } })

    detail = await jsonOf(await store.handleApexRequest(new Request('http://localhost/v1/pipelines/run-engine-recovery')))
    const recovered = await jsonOf<{ run_id: string }>(await store.handleApexRequest(new Request(`http://localhost/v1/pipelines/run-engine-recovery/gates/${detail.pending_gate.interrupt_id}/resume`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ action: 'retry' }) })))
    const events: Array<{ data: unknown }> = []
    for await (const event of store.joinRunStream('run-engine-recovery', recovered.run_id)) events.push(event)
    const recoveredDetail = await jsonOf<{ pending_gate: { phase: string }; current_phase: string; values: unknown }>(await store.handleApexRequest(new Request('http://localhost/v1/pipelines/run-engine-recovery')))
    expect(recoveredDetail).toMatchObject({ current_phase: 'execution', pending_gate: { phase: 'execution' } })
    expect(JSON.stringify(events)).toContain('apex_load.reconcile')
    expect(JSON.stringify(recoveredDetail.values)).toContain('without creating a duplicate native run')
  })

  it('makes APEX Load mutations idempotent and persists approval decisions', async () => {
    const store = createDevDataStore()
    const request = () => new Request('http://localhost/v1/apex-load/connections/conn-apex-load/scenarios', { method: 'POST', headers: { 'content-type': 'application/json', 'Idempotency-Key': 'same-scenario' }, body: JSON.stringify({ payload: { name: 'Idempotent scenario' } }) })
    const first = await jsonOf<{ id: string }>(await store.handleApexRequest(request()))
    const replay = await jsonOf<{ id: string }>(await store.handleApexRequest(request()))
    expect(replay.id).toBe(first.id)

    await store.handleApexRequest(new Request('http://localhost/v1/apex-load/connections/conn-apex-load/approvals/approval-next', { method: 'POST', headers: { 'content-type': 'application/json', 'Idempotency-Key': 'approve-next' }, body: JSON.stringify({ approved: true }) }))
    const occurrences = await jsonOf<{ items: Array<{ id: string; status: string }> }>(await store.handleApexRequest(new Request('http://localhost/v1/apex-load/connections/conn-apex-load/occurrences')))
    expect(occurrences.items.find((item) => item.id === 'occurrence-2')?.status).toBe('approved')
  })

  it('keeps context summaries out of the pipeline fleet and varies deterministic output by subject', async () => {
    const store = createDevDataStore()
    const before = store.getFixtureStats().seededRuns
    const accepted = await jsonOf<{ run_id: string; stream_url: string }>(await store.handleApexRequest(new Request('http://localhost/v1/context/summaries', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ project_id: 'telecom-modernization', subject: 'Explain the MQ queue risk' }) })))
    expect(store.getFixtureStats().seededRuns).toBe(before)
    const response = store.handleRawRequest(new Request(`http://localhost${accepted.stream_url}`))
    expect(await response?.text()).toContain('four')
  })

  it('resumes the seeded active run at execution and completes prompt-playground streams', async () => {
    const store = createDevDataStore()
    const activeEvents: Array<{ data: unknown }> = []
    for await (const event of store.joinRunStream('run-active', 'run-active-active')) {
      activeEvents.push(event)
      if (activeEvents.length === 3) break
    }
    const activePayloads = activeEvents.map((event) => event.data as { type?: string; phase?: string })
    expect(activePayloads.some((event) => event.type === 'phase_status' && event.phase === 'story_analysis')).toBe(false)
    expect(activePayloads.some((event) => event.phase === 'script_scenario' || event.phase === 'execution')).toBe(true)

    const prompts = await jsonOf<Array<{ id: string; key: string }>>(
      await store.handleApexRequest(new Request('http://localhost/v1/prompts?include_archived=true')),
    )
    const prompt = prompts.find((item) => item.key.includes('story_analysis')) ?? prompts[0]!
    const accepted = await jsonOf<{ thread_id: string; run_id: string }>(
      await store.handleApexRequest(new Request(`http://localhost/v1/prompts/${prompt.id}/test`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ sample_input: { story: 'story:9012' }, project_id: 'telecom-modernization' }),
      })),
    )
    for await (const event of store.joinRunStream(accepted.thread_id, accepted.run_id)) {
      expect(parsePipelineEvent(event.data)).not.toBeNull()
    }
    const detail = await jsonOf<{ thread_status: string; values: { prompt_playground_result: { sample_input: unknown } } }>(
      await store.handleApexRequest(new Request(`http://localhost/v1/pipelines/${accepted.thread_id}`)),
    )
    expect(detail.thread_status).toBe('idle')
    expect(detail.values.prompt_playground_result.sample_input).toEqual({ story: 'story:9012' })
  })

  it('runs the flagship gates deterministically and creates PERF-248 once', async () => {
    let store = createDevDataStore()
    await store.handleApexRequest(new Request('http://localhost/v1/work-tracking/saved-queries', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ name: 'Hydration sentinel', provider: 'jira', query: 'project = PERF', project_id: 'telecom-modernization' }),
    }))
    store = createDevDataStore()
    const thread = store.createThread({ metadata: { project_id: 'telecom-modernization' } })
    const consume = async (runId: string) => {
      const events: unknown[] = []
      for await (const event of store.joinRunStream(thread.thread_id, runId)) {
        expect(parsePipelineEvent(event.data)).not.toBeNull()
        events.push(event)
      }
      return events
    }
    let run = store.createRun(thread.thread_id, 'pipeline', { input: { title: 'ACC Add-a-Line Peak Certification' } })
    await consume(run.run_id)
    let detail = await jsonOf<{ execution_result?: string | null; pending_gate: { interrupt_id: string; phase: string } }>(await store.handleApexRequest(new Request(`http://localhost/v1/pipelines/${thread.thread_id}`)))
    expect(detail.pending_gate.phase).toBe('test_planning')
    expect(parseGateInterrupt((detail as unknown as { interrupts: Array<{ payload: unknown }> }).interrupts[0]?.payload)).not.toBeNull()

    const resume = async (interruptId: string, body: Record<string, unknown>) => jsonOf<{ run_id: string }>(await store.handleApexRequest(new Request(`http://localhost/v1/pipelines/${thread.thread_id}/gates/${interruptId}/resume`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })))
    run = { ...run, ...(await resume(detail.pending_gate.interrupt_id, { action: 'modify', prompt: { user: 'Add eligibility rejection and async coverage.' } })) }
    await consume(run.run_id)
    detail = await jsonOf(await store.handleApexRequest(new Request(`http://localhost/v1/pipelines/${thread.thread_id}`)))
    expect(detail.pending_gate.interrupt_id).not.toBe('int-prompt')

    run = { ...run, ...(await resume(detail.pending_gate.interrupt_id, { action: 'approve' })) }
    await consume(run.run_id)
    detail = await jsonOf(await store.handleApexRequest(new Request(`http://localhost/v1/pipelines/${thread.thread_id}`)))
    expect(detail.pending_gate.phase).toBe('env_triage')
    run = { ...run, ...(await resume(detail.pending_gate.interrupt_id, { action: 'discuss', message: 'Why did the MQ queue grow?' })) }
    await consume(run.run_id)
    detail = await jsonOf(await store.handleApexRequest(new Request(`http://localhost/v1/pipelines/${thread.thread_id}`)))
    expect(parseGateInterrupt((detail as unknown as { interrupts: Array<{ payload: unknown }> }).interrupts[0]?.payload)).not.toBeNull()
    expect(JSON.stringify(detail)).toContain('only two consumers are active')
    expect(JSON.stringify(detail)).toContain('non-PERFHYPER2 BSSe test endpoint')
    run = { ...run, ...(await resume(detail.pending_gate.interrupt_id, { action: 'revise', message: 'Correct the Oracle configuration and rescan.' })) }
    await consume(run.run_id)
    detail = await jsonOf(await store.handleApexRequest(new Request(`http://localhost/v1/pipelines/${thread.thread_id}`)))
    expect(JSON.stringify(detail)).toContain('PERFHYPER2 rescan is green')
    run = { ...run, ...(await resume(detail.pending_gate.interrupt_id, { action: 'approve' })) }
    await consume(run.run_id)
    detail = await jsonOf(await store.handleApexRequest(new Request(`http://localhost/v1/pipelines/${thread.thread_id}`)))
    expect(detail.pending_gate.phase).toBe('execution')
    expect(detail.execution_result).toBe('failed')
    run = { ...run, ...(await resume(detail.pending_gate.interrupt_id, { action: 'approve' })) }
    await consume(run.run_id)
    detail = await jsonOf(await store.handleApexRequest(new Request(`http://localhost/v1/pipelines/${thread.thread_id}`)))
    expect(detail.pending_gate.phase).toBe('reporting')
    const reportingInterrupt = detail.pending_gate.interrupt_id
    run = { ...run, ...(await resume(reportingInterrupt, { action: 'approve' })) }
    const immediatelyCreated = await jsonOf<{ items: Array<{ key: string }> }>(await store.handleApexRequest(new Request('http://localhost/v1/work-tracking/query/execute', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ connection_id: 'conn-jira', query: { provider: 'jira', query: 'project = PERF' }, limit: 50 }) })))
    expect(immediatelyCreated.items.some((item) => item.key === 'PERF-248')).toBe(true)
    await consume(run.run_id)
    const jira = await jsonOf<{ items: Array<{ key: string }> }>(await store.handleApexRequest(new Request('http://localhost/v1/work-tracking/query/execute', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ connection_id: 'conn-jira', query: { provider: 'jira', query: 'project = PERF' }, limit: 50 }) })))
    expect(jira.items.filter((item) => item.key === 'PERF-248')).toHaveLength(1)
    const hydrated = createDevDataStore()
    expect(await hydrated.getArtifactBytes('http://localhost/v1/artifacts/reports/PERF-248-jira-payload')?.blob.text()).toContain(thread.thread_id)
    const hydratedDetail = await jsonOf<{ values: { artifacts: Array<{ id: string }> } }>(await hydrated.handleApexRequest(new Request(`http://localhost/v1/pipelines/${thread.thread_id}`)))
    expect(hydratedDetail.values.artifacts.some((artifact) => artifact.id === 'PERF-248-jira-payload')).toBe(true)
    const stale = await store.handleApexRequest(new Request(`http://localhost/v1/pipelines/${thread.thread_id}/gates/${reportingInterrupt}/resume`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ action: 'approve' }) }))
    expect(stale?.status).toBe(409)
  }, 10_000)

  it('serves internally-consistent agent analytics that honor group_by and filters', async () => {
    const store = createDevDataStore()
    const byStage = await jsonOf<AgentAnalytics>(
      await store.handleApexRequest(
        new Request(`http://localhost/v1/analytics/agents?group_by=stage&limit=50&${WIDE_WINDOW}`),
      ),
    )

    expect(byStage.cost_visible).toBe(true)
    expect(byStage.breakdown.length).toBeGreaterThan(0)
    // A full-page breakdown partitions every event, so its token sums reconcile
    // exactly with the totals.
    const tokenSum = byStage.breakdown.reduce((sum, row) => sum + row.total_tokens, 0)
    expect(tokenSum).toBe(byStage.totals.total_tokens)

    const byAgent = await jsonOf<AgentAnalytics>(
      await store.handleApexRequest(
        new Request(`http://localhost/v1/analytics/agents?group_by=agent&limit=50&${WIDE_WINDOW}`),
      ),
    )
    expect(byAgent.breakdown.every((row) => row.key.endsWith('.worker'))).toBe(true)

    // A status filter narrows the result set.
    const errorsOnly = await jsonOf<AgentAnalytics>(
      await store.handleApexRequest(
        new Request(`http://localhost/v1/analytics/agents?group_by=stage&status=error&${WIDE_WINDOW}`),
      ),
    )
    expect(errorsOnly.totals.events).toBeLessThanOrEqual(byStage.totals.events)
    expect(errorsOnly.totals.errors).toBe(errorsOnly.totals.events)
  })
})


it('rejects retired ADO imports in demo mode without redirecting them to Jira', async () => {
  const store = createDevDataStore()
  for (const body of [
    {connection_id: 'conn-ado', query: {provider: 'ado', query: 'SELECT [System.Id] FROM WorkItems'}},
    {query: {provider: 'ado', query: 'SELECT [System.Id] FROM WorkItems'}},
  ]) {
    const response = await store.handleApexRequest(new Request('http://localhost/v1/work-tracking/query/execute', {
      method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body),
    }))
    expect(response?.status).toBe(422)
    expect(await response?.text()).toContain('Jira Align')
  }
})
