import { beforeEach, describe, expect, it } from 'vitest'

import { createDevDataStore, type DevDataStore } from '../store'

interface Snapshot {
  status: string
  sources: Array<{ role: string; repo: string; application_name: string | null; commit_sha: string }>
  entries: Array<{ id: string; role: string; repo: string }>
  snapshot_sha256: string
}
interface PipelineAifxBody {
  status: string
  snapshot: Snapshot | null
}
interface GateAifx {
  status: string
  sources: Array<{ role: string; repo: string }>
  entries: Array<{ repo: string }>
  entry_count: number
}

const BASE = 'http://localhost'
const ACC_APP = 'app-acc-perf-acc'
const MULE_APP = 'app-acc-perf-mule'
const OPUS_APP = 'app-legacy-perf-opus'
const CSI_APP = 'app-legacy-perf-csi'

async function json<T>(response: Response | null): Promise<T> {
  expect(response).not.toBeNull()
  expect((response as Response).ok).toBe(true)
  return (await (response as Response).json()) as T
}

function send(store: DevDataStore, path: string, method = 'GET', body?: unknown) {
  return store.handleApexRequest(
    new Request(`${BASE}${path}`, {
      method,
      ...(body === undefined ? {} : { headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }),
    }),
  )
}

const aifxOf = (store: DevDataStore, thread: string) => send(store, `/v1/pipelines/${thread}/aifx`).then((r) => json<PipelineAifxBody>(r))

type Participant = { id: string; project_id: string; application_id: string; environment_id: string; layer: string }

async function launch(store: DevDataStore, key: string, systems: Participant[]): Promise<string> {
  const primary = systems[0]!
  // The scope validator requires exactly one backend; both are unmapped in AIFx.
  const participants = [...systems, primary.project_id === 'legacy-perf' ? TLG : BSSE]
  const response = await send(store, '/v1/pipelines', 'POST', {
    idempotency_key: key,
    title: `AIFx ${key}`,
    request: 'Exercise AIFx pinning.',
    project_id: primary.project_id,
    app_id: primary.application_id,
    test_scope: {
      schema_version: 1,
      participants,
      primary_participant_id: primary.id,
      traffic_entry_participant_id: primary.id,
    },
  })
  expect(response?.status).toBe(202)
  return (await (response as Response).json() as { thread_id: string }).thread_id
}

const MULE: Participant = { id: 'mule', project_id: 'acc-perf', application_id: MULE_APP, environment_id: 'env-acc-perf-mule-perfhyper2', layer: 'middle' }
const ACC: Participant = { id: 'acc', project_id: 'acc-perf', application_id: ACC_APP, environment_id: 'env-acc-perf-acc-perfhyper2', layer: 'frontend' }
const OPUS: Participant = { id: 'opus', project_id: 'legacy-perf', application_id: OPUS_APP, environment_id: 'env-legacy-perf-opus-t4', layer: 'frontend' }
const CSI: Participant = { id: 'csi', project_id: 'legacy-perf', application_id: CSI_APP, environment_id: 'env-legacy-perf-csi-t4', layer: 'middle' }

const BSSE: Participant = { id: 'bsse', project_id: 'acc-perf', application_id: 'app-acc-perf-bsse', environment_id: 'env-acc-perf-bsse-perf-bsse', layer: 'backend' }
const TLG: Participant = { id: 'tlg', project_id: 'legacy-perf', application_id: 'app-legacy-perf-tlg', environment_id: 'env-legacy-perf-tlg-perf-tlg', layer: 'backend' }

const repos = (snapshot: Snapshot | null | undefined) => (snapshot?.sources ?? []).map((source) => source.repo)

describe('demo store AIFx pinning', () => {
  beforeEach(() => localStorage.clear())

  it('reports features.aifx in the demo system info', async () => {
    const store = createDevDataStore()
    expect(store.systemInfo.features).toMatchObject({ aifx: true })
    const info = await json<{ features: Record<string, unknown> }>(await send(store, '/v1/system/info'))
    expect(info.features.aifx).toBe(true)
  })

  it('carries an OPUS AIFx preview on the seeded prompt-review gate payload', async () => {
    const store = createDevDataStore()
    const detail = await json<{ interrupts: Array<{ kind: string; payload: { aifx?: GateAifx } }> }>(
      await send(store, '/v1/pipelines/run-prompt-review'),
    )
    const gate = detail.interrupts.find((item) => item.kind === 'prompt_review')
    const aifx = gate?.payload.aifx
    expect(aifx?.status).toBe('pinned')
    expect(aifx?.sources.map((source) => source.repo)).toEqual([
      'aifx/global-rules',
      'aifx/apm0067890-team-documents',
      'aifx/apm0067890-acb',
    ])
    expect(aifx?.sources.some((source) => source.repo.includes('apm0012345'))).toBe(false)
    expect(aifx?.entry_count).toBe(aifx?.entries.length)
    expect(aifx?.entry_count).toBeGreaterThan(0)
    // test_planning-applicable entries only: every repo contributes at least one.
    expect(new Set(aifx?.entries.map((entry) => entry.repo))).toEqual(new Set(aifx?.sources.map((source) => source.repo)))
  })

  it('returns the ACC snapshot for a seeded ACC run', async () => {
    const store = createDevDataStore()
    const body = await aifxOf(store, 'run-active')
    expect(body.status).toBe('pinned')
    expect(repos(body.snapshot)).toEqual(['aifx/global-rules', 'aifx/apm0012345-team-documents', 'aifx/apm0012345-acb'])
    expect(body.snapshot?.sources.find((source) => source.role === 'team-documents')?.application_name).toBe('ACC')
    expect(body.snapshot?.entries.length).toBeGreaterThan(0)
  })

  it('404s the AIFx read for an unknown thread', async () => {
    const store = createDevDataStore()
    expect((await send(store, '/v1/pipelines/no-such-thread/aifx'))?.status).toBe(404)
  })

  // SOURCE DEFECT: createThread() builds launched runs via pipelineDetail(), which
  // stamps values.demo_profile = 'acc-flagship'. aifxApplicationsOf() checks
  // demo_profile before run_config.test_scope, so every launched run resolves to
  // ACC's participants and pins ACC's snapshot regardless of its own scope.
  it('pins nothing for a launched run whose systems are all unonboarded', async () => {
    const store = createDevDataStore()
    const muleOnly = await launch(store, 'mule-only', [MULE])
    expect(await aifxOf(store, muleOnly)).toEqual({ status: 'none', snapshot: null })
    const csiOnly = await launch(store, 'csi-only', [CSI])
    expect(await aifxOf(store, csiOnly)).toEqual({ status: 'none', snapshot: null })
  })

  it('pins global rules and participant ACB but no team documents when the primary is unmapped', async () => {
    const store = createDevDataStore()
    // Unonboarded primary with an onboarded participant: global rules and that
    // participant's ACB, but never the participant's team documents.
    const mulePrimary = await launch(store, 'mule-with-acc', [MULE, ACC])
    const mixed = await aifxOf(store, mulePrimary)
    const roles = (mixed.snapshot?.sources ?? []).map((source) => source.role)
    expect(roles).not.toContain('team-documents')
    expect(repos(mixed.snapshot)).toEqual(['aifx/global-rules', 'aifx/apm0012345-acb'])
  })

  it('pins OPUS repositories for a launched run whose primary is OPUS', async () => {
    const store = createDevDataStore()
    const thread = await launch(store, 'opus-primary', [OPUS, CSI])
    const body = await aifxOf(store, thread)
    expect(body.status).toBe('pinned')
    expect(repos(body.snapshot)).toEqual(['aifx/global-rules', 'aifx/apm0067890-team-documents', 'aifx/apm0067890-acb'])
    expect(repos(body.snapshot).some((repo) => repo.includes('apm0012345'))).toBe(false)
  })

  it('freezes the pinned snapshot on first read while new runs see mapping edits', async () => {
    const store = createDevDataStore()
    const first = await launch(store, 'freeze-before', [ACC])
    const original = await aifxOf(store, first)
    expect(repos(original.snapshot)).toContain('aifx/apm0012345-acb')

    const current = await json<Record<string, unknown>>(await send(store, `/v1/admin/aifx/applications/${ACC_APP}`))
    const updated = await json<{ acb_repo: string }>(
      await send(store, `/v1/admin/aifx/applications/${ACC_APP}`, 'PUT', { ...current, acb_repo: 'acc-renamed-acb' }),
    )
    expect(updated.acb_repo).toBe('acc-renamed-acb')

    expect(await aifxOf(store, first)).toEqual(original)

    const second = await launch(store, 'freeze-after', [ACC])
    const changed = await aifxOf(store, second)
    expect(repos(changed.snapshot)).toContain('aifx/acc-renamed-acb')
    expect(repos(changed.snapshot)).not.toContain('aifx/apm0012345-acb')
    expect(changed.snapshot?.snapshot_sha256).toBeDefined()
  })
})
