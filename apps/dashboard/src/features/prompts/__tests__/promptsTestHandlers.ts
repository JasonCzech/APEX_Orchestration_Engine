/**
 * msw fixtures for the prompt catalog suites — a tiny stateful in-memory
 * catalog so save-version / rollback / archive flows observe the same
 * pointer-move semantics as src/apex/services/prompts.py.
 */
import { http, HttpResponse } from 'msw'

export interface VersionRecord {
  id: string
  version: number
  content: string
  note: string | null
  created_by: string | null
  created_at: string
  parent_version_id: string | null
}

export interface PromptRecord {
  id: string
  namespace: string
  key: string
  description: string | null
  archived_at: string | null
  updated_at: string
  active_version_id: string
  versions: VersionRecord[]
}

export const STORY_V1_CONTENT = 'You are a story analyst.\nBe terse.\nReturn bullet points.'
export const STORY_V2_CONTENT =
  'You are a story analyst.\nBe thorough.\nReturn bullet points.\nCite evidence for every claim.'

function storyPrompt(): PromptRecord {
  return {
    id: 'p-story',
    namespace: 'phase',
    key: 'story_analysis/system',
    description: 'System prompt for story analysis',
    archived_at: null,
    updated_at: '2026-06-10T10:00:00Z',
    active_version_id: 'v-2',
    versions: [
      {
        id: 'v-1',
        version: 1,
        content: STORY_V1_CONTENT,
        note: 'initial draft',
        created_by: 'alice',
        created_at: '2026-06-01T10:00:00Z',
        parent_version_id: null,
      },
      {
        id: 'v-2',
        version: 2,
        content: STORY_V2_CONTENT,
        note: 'tighten tone',
        created_by: 'bob',
        created_at: '2026-06-08T10:00:00Z',
        parent_version_id: 'v-1',
      },
    ],
  }
}

function execPrompt(): PromptRecord {
  return {
    id: 'p-exec',
    namespace: 'phase',
    key: 'execution/system',
    description: 'Execution-phase system prompt',
    archived_at: null,
    updated_at: '2026-06-09T10:00:00Z',
    active_version_id: 'v-exec-1',
    versions: [
      {
        id: 'v-exec-1',
        version: 1,
        content: 'Run the plan.',
        note: null,
        created_by: 'alice',
        created_at: '2026-06-02T10:00:00Z',
        parent_version_id: null,
      },
    ],
  }
}

function opsPrompt(): PromptRecord {
  return {
    id: 'p-ops',
    namespace: 'ops',
    key: 'summarize/system',
    description: 'Ops summary prompt',
    archived_at: null,
    updated_at: '2026-06-05T10:00:00Z',
    active_version_id: 'v-ops-1',
    versions: [
      {
        id: 'v-ops-1',
        version: 1,
        content: 'Summarize the incident.',
        note: null,
        created_by: 'carol',
        created_at: '2026-06-03T10:00:00Z',
        parent_version_id: null,
      },
    ],
  }
}

function retiredPrompt(): PromptRecord {
  return {
    id: 'p-retired',
    namespace: 'ops',
    key: 'retired/system',
    description: 'Old escalation prompt',
    archived_at: '2026-05-20T10:00:00Z',
    updated_at: '2026-05-20T10:00:00Z',
    active_version_id: 'v-ret-1',
    versions: [
      {
        id: 'v-ret-1',
        version: 1,
        content: 'Escalate loudly.',
        note: null,
        created_by: 'carol',
        created_at: '2026-05-01T10:00:00Z',
        parent_version_id: null,
      },
    ],
  }
}

interface CapturedCalls {
  create: unknown[]
  saveVersion: unknown[]
  rollback: unknown[]
  test: unknown[]
  archive: number
  unarchive: number
  listRequests: string[]
}

function summaryOf(prompt: PromptRecord) {
  const active = prompt.versions.find((entry) => entry.id === prompt.active_version_id)
  return {
    id: prompt.id,
    namespace: prompt.namespace,
    key: prompt.key,
    description: prompt.description,
    active_version: active ? { id: active.id, version: active.version } : null,
    archived_at: prompt.archived_at,
    updated_at: prompt.updated_at,
  }
}

function detailOf(prompt: PromptRecord) {
  const active = prompt.versions.find((entry) => entry.id === prompt.active_version_id)
  return {
    ...summaryOf(prompt),
    content: active?.content ?? null,
    note: active?.note ?? null,
  }
}

function versionInfoOf(version: VersionRecord) {
  return {
    id: version.id,
    version: version.version,
    note: version.note,
    created_by: version.created_by,
    created_at: version.created_at,
    parent_version_id: version.parent_version_id,
  }
}

/**
 * Fresh catalog state + handlers per test. `accept` controls the playground
 * 202 payload; `register` the result with server.use(...handlers).
 */
export function promptCatalog(
  options: { accept?: { run_id: string; thread_id: string | null } } = {},
) {
  const prompts: PromptRecord[] = [storyPrompt(), execPrompt(), opsPrompt(), retiredPrompt()]
  const accept = options.accept ?? { run_id: 'run-1234', thread_id: 'thread-9' }
  const calls: CapturedCalls = {
    create: [],
    saveVersion: [],
    rollback: [],
    test: [],
    archive: 0,
    unarchive: 0,
    listRequests: [],
  }

  const find = (id: string | readonly string[] | undefined) =>
    prompts.find((entry) => entry.id === id)

  const handlers = [
    http.get('*/v1/prompts', ({ request }) => {
      const url = new URL(request.url)
      calls.listRequests.push(url.search)
      const namespace = url.searchParams.get('namespace')
      const includeArchived = url.searchParams.get('include_archived') === 'true'
      const q = url.searchParams.get('q')?.toLowerCase()
      let rows = prompts
      if (namespace) rows = rows.filter((entry) => entry.namespace === namespace)
      if (!includeArchived) rows = rows.filter((entry) => !entry.archived_at)
      if (q) {
        rows = rows.filter(
          (entry) =>
            entry.key.toLowerCase().includes(q) ||
            (entry.description ?? '').toLowerCase().includes(q),
        )
      }
      return HttpResponse.json(rows.map(summaryOf))
    }),

    http.post('*/v1/prompts', async ({ request }) => {
      const body = (await request.json()) as {
        namespace: string
        key: string
        content: string
        description?: string | null
        note?: string | null
      }
      calls.create.push(body)
      const created: PromptRecord = {
        id: 'p-new',
        namespace: body.namespace,
        key: body.key,
        description: body.description ?? null,
        archived_at: null,
        updated_at: '2026-06-11T10:00:00Z',
        active_version_id: 'v-new-1',
        versions: [
          {
            id: 'v-new-1',
            version: 1,
            content: body.content,
            note: body.note ?? null,
            created_by: 'dash-ops',
            created_at: '2026-06-11T10:00:00Z',
            parent_version_id: null,
          },
        ],
      }
      prompts.push(created)
      return HttpResponse.json(detailOf(created), { status: 201 })
    }),

    http.get('*/v1/prompts/:promptId', ({ params }) => {
      const prompt = find(params.promptId)
      if (!prompt) return HttpResponse.json({ detail: 'prompt not found' }, { status: 404 })
      return HttpResponse.json(detailOf(prompt))
    }),

    http.get('*/v1/prompts/:promptId/versions', ({ params }) => {
      const prompt = find(params.promptId)
      if (!prompt) return HttpResponse.json({ detail: 'prompt not found' }, { status: 404 })
      return HttpResponse.json(prompt.versions.map(versionInfoOf))
    }),

    http.get('*/v1/prompts/:promptId/versions/:versionId', ({ params }) => {
      const prompt = find(params.promptId)
      const version = prompt?.versions.find((entry) => entry.id === params.versionId)
      if (!prompt || !version) {
        return HttpResponse.json({ detail: 'version not found' }, { status: 404 })
      }
      return HttpResponse.json(version)
    }),

    http.post('*/v1/prompts/:promptId/versions', async ({ params, request }) => {
      const prompt = find(params.promptId)
      if (!prompt) return HttpResponse.json({ detail: 'prompt not found' }, { status: 404 })
      const body = (await request.json()) as { content: string; note?: string | null }
      calls.saveVersion.push(body)
      const next: VersionRecord = {
        id: `v-${prompt.id}-${prompt.versions.length + 1}`,
        version: Math.max(...prompt.versions.map((entry) => entry.version)) + 1,
        content: body.content,
        note: body.note ?? null,
        created_by: 'dash-ops',
        created_at: '2026-06-11T11:00:00Z',
        parent_version_id: prompt.active_version_id,
      }
      prompt.versions.push(next)
      prompt.active_version_id = next.id
      prompt.updated_at = next.created_at
      return HttpResponse.json(next, { status: 201 })
    }),

    http.post('*/v1/prompts/:promptId/rollback', async ({ params, request }) => {
      const prompt = find(params.promptId)
      if (!prompt) return HttpResponse.json({ detail: 'prompt not found' }, { status: 404 })
      const body = (await request.json()) as { version_id: string }
      calls.rollback.push(body)
      const target = prompt.versions.find((entry) => entry.id === body.version_id)
      if (!target) {
        return HttpResponse.json({ detail: 'version belongs to another prompt' }, { status: 409 })
      }
      prompt.active_version_id = target.id
      return HttpResponse.json(detailOf(prompt))
    }),

    http.post('*/v1/prompts/:promptId/archive', ({ params }) => {
      const prompt = find(params.promptId)
      if (!prompt) return HttpResponse.json({ detail: 'prompt not found' }, { status: 404 })
      calls.archive += 1
      prompt.archived_at = '2026-06-11T12:00:00Z'
      return HttpResponse.json(summaryOf(prompt))
    }),

    http.post('*/v1/prompts/:promptId/unarchive', ({ params }) => {
      const prompt = find(params.promptId)
      if (!prompt) return HttpResponse.json({ detail: 'prompt not found' }, { status: 404 })
      calls.unarchive += 1
      prompt.archived_at = null
      return HttpResponse.json(summaryOf(prompt))
    }),

    http.post('*/v1/prompts/:promptId/test', async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>
      calls.test.push(body)
      return HttpResponse.json(accept, { status: 202 })
    }),
  ]

  return { handlers, calls, prompts }
}
