import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { RequireGlobalAdmin } from '@/auth/RequireRole'
import { executionLink } from './executionLink'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { ProblemCard } from '@/components/ProblemCard'
import { getDurableIdempotencyKey, retireDurableIdempotencyKey } from '@/utils/durableIdempotency'

import '../admin/admin.css'

type NativeObject = Record<string, unknown>
type NativeResource = components['schemas']['NativeResourceProjection']
type NativeCapabilities = components['schemas']['NativeCapabilitiesResponse']
type NativeMirroredRun = components['schemas']['NativeMirroredRunProjection']

const SCENARIO_IDEMPOTENCY_STORAGE_KEY = 'apex.idempotency.apex-load-scenario.v1'
const SCHEDULE_IDEMPOTENCY_STORAGE_KEY = 'apex.idempotency.apex-load-schedule.v1'

interface NativeConnection {
  id: string
  name: string
  project_id: string
}

interface NativeCollection<T> {
  items: T[]
  truncated: boolean
}

function object(value: unknown): NativeObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as NativeObject)
    : {}
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null
}

function createIdempotencyKey(): string {
  return typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `apex-load-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function nativeError(response: Response, error: unknown, label: string): ApiError {
  return new ApiError(
    response.status,
    errorMessageOf(error, `${label} failed (${response.status})`),
    error,
  )
}

async function fetchConnections(): Promise<NativeCollection<NativeConnection>> {
  const { data, error, response } = await getApexClient().GET('/v1/apex-load/connections')
  if (!response.ok || !data) throw nativeError(response, error, 'APEX Load connections')
  const items = object(data).items
  if (!Array.isArray(items)) return { items: [], truncated: false }
  return {
    items: items.flatMap((item) => {
      const value = object(item)
      const id = text(value.id)
      const name = text(value.name)
      const projectId = text(value.project_id)
      return id && name && projectId ? [{ id, name, project_id: projectId }] : []
    }),
    truncated: data.truncated,
  }
}

async function fetchNativeObject(
  connectionId: string,
  resource: 'schedule-health' | 'calendar',
): Promise<NativeObject> {
  const path = `/v1/apex-load/connections/{connection_id}/${resource}` as const
  const { data, error, response } = await getApexClient().GET(path, {
    params: { path: { connection_id: connectionId } },
  })
  if (!response.ok || !data) throw nativeError(response, error, `APEX Load ${resource}`)
  return object(data.details)
}

async function fetchCapabilities(connectionId: string): Promise<NativeCapabilities> {
  const { data, error, response } = await getApexClient().GET(
    '/v1/apex-load/connections/{connection_id}/capabilities',
    { params: { path: { connection_id: connectionId } } },
  )
  if (!response.ok || !data) throw nativeError(response, error, 'APEX Load capabilities')
  return data
}

async function fetchNativeResources(
  connectionId: string,
  resource: 'scenarios' | 'runs',
): Promise<NativeCollection<NativeResource>> {
  const client = getApexClient()
  const result =
    resource === 'runs'
      ? await client.GET('/v1/apex-load/connections/{connection_id}/runs', {
          params: { path: { connection_id: connectionId } },
        })
      : await client.GET('/v1/apex-load/connections/{connection_id}/scenarios', {
          params: { path: { connection_id: connectionId } },
        })
  if (!result.response.ok || !result.data) {
    throw nativeError(result.response, result.error, `APEX Load ${resource}`)
  }
  return { items: result.data.items, truncated: result.data.truncated }
}

async function fetchMirroredRuns(connectionId: string): Promise<NativeMirroredRun[]> {
  const { data, error, response } = await getApexClient().GET(
    '/v1/apex-load/connections/{connection_id}/mirrored-runs',
    { params: { path: { connection_id: connectionId }, query: { limit: 25, offset: 0 } } },
  )
  if (!response.ok || !data) throw nativeError(response, error, 'APEX Load mirrored runs')
  return data.items
}

async function fetchSchedules(
  connectionId: string,
  scenarioId: string,
): Promise<NativeCollection<NativeResource>> {
  const { data, error, response } = await getApexClient().GET(
    '/v1/apex-load/connections/{connection_id}/scenarios/{scenario_id}/schedules',
    { params: { path: { connection_id: connectionId, scenario_id: scenarioId } } },
  )
  if (!response.ok || !data) throw nativeError(response, error, 'APEX Load schedules')
  return { items: data.items, truncated: data.truncated }
}

function parseDraft(value: string): NativeObject {
  const parsed: unknown = JSON.parse(value)
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Draft JSON must be an object.')
  }
  return parsed as NativeObject
}

function JsonPanel({
  title,
  value,
  error,
  pending = false,
  onRetry,
}: {
  title: string
  value: unknown
  error?: Error | null
  pending?: boolean
  onRetry?: () => void
}) {
  if (error) {
    return <ProblemCard title={`${title} unavailable`} message={error.message} onRetry={onRetry} />
  }
  return (
    <details className="adm-panel glass-panel">
      <summary className="adm-panel-title">{title}</summary>
      <pre className="adm-json-input">{pending ? 'Loading…' : JSON.stringify(value, null, 2)}</pre>
    </details>
  )
}

export function ApexLoadWorkspacePage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const requestedConnection = searchParams.get('connection')
  const requestedProject = searchParams.get('project')
  const requestedRun = searchParams.get('run')
  const queryClient = useQueryClient()
  const connections = useQuery({
    queryKey: ['apex-load', 'connections'],
    queryFn: fetchConnections,
  })
  const [connectionId, setConnectionId] = useState('')
  const [scenarioId, setScenarioId] = useState('')
  const [scheduleId, setScheduleId] = useState('')
  const [selectedRun, setSelectedRun] = useState(searchParams.get('run') ?? '')
  const [workspaceView, setWorkspaceView] = useState<'executions' | 'authoring'>('executions')
  const [approvalId, setApprovalId] = useState('')
  const [evidenceSignature, setEvidenceSignature] = useState('')
  const [scenarioDraft, setScenarioDraft] = useState('{\n  "name": "New scenario"\n}')
  const [scheduleDraft, setScheduleDraft] = useState('{\n  "name": "Scheduled run"\n}')

  useEffect(() => {
    const availableConnections = connections.data?.items
    if (!availableConnections) return
    const candidates = availableConnections.filter(
      (connection) =>
        (!requestedConnection || connection.id === requestedConnection) &&
        (!requestedProject || connection.project_id === requestedProject),
    )
    const nextConnectionId = candidates.length === 1
      ? candidates[0]!.id
      : !requestedConnection && !requestedProject && candidates.some(item => item.id === connectionId)
        ? connectionId
        : ''
    if (nextConnectionId !== connectionId) {
      setConnectionId(nextConnectionId)
      setScenarioId('')
      setScheduleId('')
      setApprovalId('')
      setEvidenceSignature('')
    }
    setSelectedRun(requestedRun ?? '')
  }, [connections.data, connectionId, requestedConnection, requestedProject, requestedRun])

  const capabilities = useQuery({
    queryKey: ['apex-load', connectionId, 'capabilities'],
    queryFn: () => fetchCapabilities(connectionId),
    enabled: Boolean(connectionId),
  })
  const scenarios = useQuery({
    queryKey: ['apex-load', connectionId, 'scenarios'],
    queryFn: () => fetchNativeResources(connectionId, 'scenarios'),
    enabled: Boolean(connectionId),
  })
  const nativeRuns = useQuery({
    queryKey: ['apex-load', connectionId, 'runs'],
    queryFn: () => fetchNativeResources(connectionId, 'runs'),
    enabled: Boolean(connectionId),
    refetchInterval: 10_000,
  })
  const mirroredRuns = useQuery({
    queryKey: ['apex-load', connectionId, 'mirrored-runs'],
    queryFn: () => fetchMirroredRuns(connectionId),
    enabled: Boolean(connectionId),
    refetchInterval: 10_000,
  })
  const scheduleHealth = useQuery({
    queryKey: ['apex-load', connectionId, 'schedule-health'],
    queryFn: () => fetchNativeObject(connectionId, 'schedule-health'),
    enabled: Boolean(connectionId),
  })
  const calendar = useQuery({
    queryKey: ['apex-load', connectionId, 'calendar'],
    queryFn: () => fetchNativeObject(connectionId, 'calendar'),
    enabled: Boolean(connectionId),
  })
  const schedules = useQuery({
    queryKey: ['apex-load', connectionId, scenarioId, 'schedules'],
    queryFn: () => fetchSchedules(connectionId, scenarioId),
    enabled: Boolean(connectionId && scenarioId),
  })
  const occurrences = useQuery({
    queryKey: ['apex-load', connectionId, scenarioId, scheduleId, 'occurrences'],
    queryFn: async () => {
      const { data, error, response } = await getApexClient().GET(
        '/v1/apex-load/connections/{connection_id}/scenarios/{scenario_id}/schedules/{schedule_id}/occurrences',
        {
          params: {
            path: { connection_id: connectionId, scenario_id: scenarioId, schedule_id: scheduleId },
          },
        },
      )
      if (!response.ok || !data)
        throw nativeError(response, error, 'APEX Load schedule occurrences')
      return data.items
    },
    enabled: Boolean(connectionId && scenarioId && scheduleId),
  })
  const runMetrics = useQuery({
    queryKey: ['apex-load', connectionId, selectedRun, 'metrics'],
    queryFn: async () => {
      const { data, error, response } = await getApexClient().GET(
        '/v1/apex-load/connections/{connection_id}/runs/{run_id}/metrics',
        { params: { path: { connection_id: connectionId, run_id: selectedRun } } },
      )
      if (!response.ok || !data) throw nativeError(response, error, 'APEX Load run metrics')
      return object(data.details)
    },
    enabled: Boolean(connectionId && selectedRun),
    refetchInterval: 5_000,
  })
  const runEvidence = useQuery({
    queryKey: ['apex-load', connectionId, selectedRun, evidenceSignature, 'evidence'],
    queryFn: async () => {
      const { data, error, response } = await getApexClient().GET(
        '/v1/apex-load/connections/{connection_id}/runs/{run_id}/evidence',
        {
          params: {
            path: { connection_id: connectionId, run_id: selectedRun },
            query: { signature: evidenceSignature },
          },
        },
      )
      if (!response.ok || !data) throw nativeError(response, error, 'APEX Load run evidence')
      return object(data.details)
    },
    enabled: Boolean(connectionId && selectedRun && evidenceSignature),
  })

  useEffect(() => {
    if (!scenarios.data) return
    const ids = scenarios.data.items
      .map((scenario) => text(scenario.id))
      .filter((id): id is string => Boolean(id))
    if (!ids.includes(scenarioId)) {
      setScenarioId(ids[0] ?? '')
      setScheduleId('')
      setApprovalId('')
    }
  }, [scenarioId, scenarios.data])

  useEffect(() => {
    if (!schedules.data) return
    const ids = schedules.data.items
      .map((schedule) => text(schedule.id))
      .filter((id): id is string => Boolean(id))
    if (!ids.includes(scheduleId)) {
      setScheduleId(ids[0] ?? '')
      setApprovalId('')
    }
  }, [scheduleId, schedules.data])

  useEffect(() => {
    if (!nativeRuns.data || !selectedRun) return
    if (!nativeRuns.data.items.some((run) => run.id === selectedRun)) {
      setSelectedRun('')
      setEvidenceSignature('')
    }
  }, [nativeRuns.data, selectedRun])

  const validateScenario = useMutation({
    mutationFn: async () => {
      const { data, error, response } = await getApexClient().POST(
        '/v1/apex-load/connections/{connection_id}/scenarios/validation',
        {
          params: { path: { connection_id: connectionId } },
          body: { payload: parseDraft(scenarioDraft) },
        },
      )
      if (!response.ok || !data) throw nativeError(response, error, 'APEX Load scenario validation')
      return object(data.details)
    },
  })
  const sizeScenario = useMutation({
    mutationFn: async () => {
      const { data, error, response } = await getApexClient().POST(
        '/v1/apex-load/connections/{connection_id}/scenarios/sizing',
        {
          params: { path: { connection_id: connectionId } },
          body: { payload: parseDraft(scenarioDraft) },
        },
      )
      if (!response.ok || !data) throw nativeError(response, error, 'APEX Load scenario sizing')
      return object(data.details)
    },
  })
  const createScenario = useMutation({
    mutationFn: async () => {
      const requestConnectionId = connectionId
      const payload = parseDraft(scenarioDraft)
      const intent = { connectionId: requestConnectionId, payload }
      const idempotencyKey = await getDurableIdempotencyKey(
        SCENARIO_IDEMPOTENCY_STORAGE_KEY,
        intent,
        createIdempotencyKey,
      )
      const { data, error, response } = await getApexClient().POST(
        '/v1/apex-load/connections/{connection_id}/scenarios',
        {
          params: {
            path: { connection_id: requestConnectionId },
            header: { 'Idempotency-Key': idempotencyKey },
          },
          body: { payload },
        },
      )
      if (!response.ok || !data) throw nativeError(response, error, 'APEX Load scenario create')
      await retireDurableIdempotencyKey(SCENARIO_IDEMPOTENCY_STORAGE_KEY, intent, idempotencyKey)
      return { connectionId: requestConnectionId, data }
    },
    onSuccess: ({ connectionId: completedConnectionId }) => {
      void queryClient.invalidateQueries({
        queryKey: ['apex-load', completedConnectionId],
      })
    },
  })
  const createSchedule = useMutation({
    mutationFn: async () => {
      const requestConnectionId = connectionId
      const requestScenarioId = scenarioId
      const payload = parseDraft(scheduleDraft)
      const intent = {
        connectionId: requestConnectionId,
        scenarioId: requestScenarioId,
        payload,
      }
      const idempotencyKey = await getDurableIdempotencyKey(
        SCHEDULE_IDEMPOTENCY_STORAGE_KEY,
        intent,
        createIdempotencyKey,
      )
      const { data, error, response } = await getApexClient().POST(
        '/v1/apex-load/connections/{connection_id}/scenarios/{scenario_id}/schedules',
        {
          params: {
            path: {
              connection_id: requestConnectionId,
              scenario_id: requestScenarioId,
            },
            header: { 'Idempotency-Key': idempotencyKey },
          },
          body: { payload },
        },
      )
      if (!response.ok || !data) throw nativeError(response, error, 'APEX Load schedule create')
      await retireDurableIdempotencyKey(SCHEDULE_IDEMPOTENCY_STORAGE_KEY, intent, idempotencyKey)
      return { connectionId: requestConnectionId, data }
    },
    onSuccess: ({ connectionId: completedConnectionId }) => {
      void queryClient.invalidateQueries({
        queryKey: ['apex-load', completedConnectionId],
      })
    },
  })
  const decideScheduleApproval = useMutation({
    mutationFn: async (approved: boolean) => {
      if (!scenarioId || !scheduleId || !approvalId) {
        throw new Error('Choose a scenario, schedule, and approval id first.')
      }
      const requestConnectionId = connectionId
      const { data, error, response } = await getApexClient().POST(
        '/v1/apex-load/connections/{connection_id}/scenarios/{scenario_id}/schedules/{schedule_id}/approvals/{approval_id}',
        {
          params: {
            path: {
              connection_id: requestConnectionId,
              scenario_id: scenarioId,
              schedule_id: scheduleId,
              approval_id: approvalId,
            },
          },
          body: { approved },
        },
      )
      if (!response.ok || !data) throw nativeError(response, error, 'APEX Load schedule approval')
      return { connectionId: requestConnectionId, data }
    },
    onSuccess: ({ connectionId: completedConnectionId }) => {
      void queryClient.invalidateQueries({
        queryKey: ['apex-load', completedConnectionId],
      })
    },
  })

  const resetValidation = validateScenario.reset
  const resetSizing = sizeScenario.reset
  const resetScenarioCreate = createScenario.reset
  const resetScheduleCreate = createSchedule.reset
  const resetApproval = decideScheduleApproval.reset

  useEffect(() => {
    resetValidation()
    resetSizing()
    resetScenarioCreate()
  }, [connectionId, resetScenarioCreate, resetSizing, resetValidation, scenarioDraft])

  useEffect(() => {
    resetScheduleCreate()
  }, [connectionId, resetScheduleCreate, scenarioId, scheduleDraft])

  useEffect(() => {
    resetApproval()
  }, [approvalId, connectionId, resetApproval, scenarioId, scheduleId])

  const activeConnection = useMemo(
    () => connections.data?.items.find((connection) => connection.id === connectionId),
    [connectionId, connections.data],
  )

  if (connections.isPending) return <div className="dash-empty">Loading APEX Load connections…</div>
  if (connections.isError) {
    return (
      <ProblemCard
        title="APEX Load unavailable"
        message={connections.error.message}
        onRetry={() => void connections.refetch()}
      />
    )
  }
  if (!connections.data?.items.length) {
    return (
      <section className="glass-panel ux-card">
        <h2>Connect APEX Load</h2>
        <p>No enabled, project-scoped APEX Load connection is available to your account.</p>
        <p>
          An administrator can enable a connection and map your project and application to APEX Load. Use Manage connections if it is available below; otherwise ask your administrator.
        </p>
        <div className="ux-actions">
          <RequireGlobalAdmin>
            <Link className="btn btn-primary" to="/admin/connections">
              Manage connections
            </Link>
          </RequireGlobalAdmin>
          <button className="btn btn-secondary" onClick={() => void connections.refetch()}>
            Refresh connections
          </button>
        </div>
      </section>
    )
  }

  return (
    <div className="adm-page" data-testid="apex-load-workspace">
      <header className="adm-toolbar glass-panel">
        <div>
          <h2 className="adm-page-title">APEX Load workspace</h2>
          <span className="adm-card-time">
            A connection workspace for scenarios, schedules, executions, and evidence. Connection setup remains in Connections.
          </span>
        </div>
        <label className="adm-field">
          <span className="adm-field-label">Connection</span>
          <select
            value={connectionId}
            onChange={(event) => {
              setConnectionId(event.target.value)
              setSearchParams(event.target.value ? { connection: event.target.value } : {})
              setScenarioId('')
              setScheduleId('')
              setSelectedRun('')
              setApprovalId('')
              setEvidenceSignature('')
            }}
          >
            <option value="">Choose a connection</option>
            {connections.data.items.map((connection) => (
              <option key={connection.id} value={connection.id}>
                {connection.name} · {connection.project_id}
              </option>
            ))}
          </select>
        </label>
      </header>
      <section className="glass-panel ux-card" aria-label="Connection readiness">
        <h3>{activeConnection ? 'Project connection selected' : 'Choose a project connection'}</h3>
        <p>
          {activeConnection
            ? `${activeConnection.name} · Project ${activeConnection.project_id}`
            : 'Select the connection mapped to your project. No connection is selected automatically when the requested context is unavailable or ambiguous.'}
        </p>
        <p>
          APEX Load executes workloads and owns schedules. Orchestration collects evidence and runs
          analysis.
        </p>
        <RequireGlobalAdmin>
          <Link to="/admin/connections">Manage connections and mappings</Link>
        </RequireGlobalAdmin>
      </section>
      {connections.data.truncated && (
        <p className="adm-field-help" role="status">
          The connection list reached its display limit; narrow the operator scope to see omitted
          connections.
        </p>
      )}

      {activeConnection && (
        <>
          <div className="adm-tabs" role="tablist" aria-label="Load integration sections">
            <button
              type="button"
              role="tab"
              aria-selected={workspaceView === 'executions'}
              className={workspaceView === 'executions' ? 'adm-tab active' : 'adm-tab'}
              onClick={() => setWorkspaceView('executions')}
            >
              Executions & analysis
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={workspaceView === 'authoring'}
              className={workspaceView === 'authoring' ? 'adm-tab active' : 'adm-tab'}
              onClick={() => setWorkspaceView('authoring')}
            >
              Scenarios & schedules
            </button>
          </div>

          {workspaceView === 'authoring' && <>
          {activeConnection && (
            <JsonPanel
              title="Load-model templates and capabilities"
              value={capabilities.data ?? {}}
              error={capabilities.error}
              pending={capabilities.isPending}
              onRetry={() => void capabilities.refetch()}
            />
          )}
          <JsonPanel
            title="Schedule health"
            value={scheduleHealth.data ?? {}}
            error={scheduleHealth.error}
            pending={scheduleHealth.isPending}
            onRetry={() => void scheduleHealth.refetch()}
          />
          <JsonPanel
            title="Native schedule calendar"
            value={calendar.data ?? {}}
            error={calendar.error}
            pending={calendar.isPending}
            onRetry={() => void calendar.refetch()}
          />

          <section className="adm-panel glass-panel">
            <h3 className="adm-panel-title">Scenarios and load-model validation</h3>
            <textarea
              className="adm-json-input"
              aria-label="Scenario JSON"
              rows={8}
              value={scenarioDraft}
              onChange={(event) => setScenarioDraft(event.target.value)}
            />
            <div className="adm-panel-actions">
              <button
                className="btn btn-secondary btn-sm"
                type="button"
                disabled={!connectionId || validateScenario.isPending}
                onClick={() => validateScenario.mutate()}
              >
                Validate scenario
              </button>
              <button
                className="btn btn-secondary btn-sm"
                type="button"
                disabled={!connectionId || sizeScenario.isPending}
                onClick={() => sizeScenario.mutate()}
              >
                Estimate capacity
              </button>
              <button
                className="btn btn-primary btn-sm"
                type="button"
                disabled={!connectionId || createScenario.isPending}
                onClick={() => createScenario.mutate()}
              >
                Create scenario
              </button>
            </div>
            {(validateScenario.data || validateScenario.error) && (
              <pre className="adm-json-input">
                {validateScenario.error instanceof Error
                  ? validateScenario.error.message
                  : JSON.stringify(validateScenario.data, null, 2)}
              </pre>
            )}
            {(sizeScenario.data || sizeScenario.error) && (
              <pre className="adm-json-input">
                {sizeScenario.error instanceof Error
                  ? sizeScenario.error.message
                  : JSON.stringify(sizeScenario.data, null, 2)}
              </pre>
            )}
            {(createScenario.data || createScenario.error) && (
              <pre className="adm-json-input">
                {createScenario.error instanceof Error
                  ? createScenario.error.message
                  : JSON.stringify(createScenario.data?.data, null, 2)}
              </pre>
            )}
            {scenarios.isError && (
              <ProblemCard
                title="Scenarios unavailable"
                message={scenarios.error.message}
                onRetry={() => void scenarios.refetch()}
              />
            )}
            <ul className="data-table">
              {scenarios.data?.items.map((scenario) => {
                const id = text(scenario.id) ?? 'unknown'
                return (
                  <li key={id}>
                    <button
                      className="btn btn-ghost btn-sm"
                      type="button"
                      onClick={() => {
                        setScenarioId(id)
                        setScheduleId('')
                        setApprovalId('')
                      }}
                    >
                      {text(scenario.name) ?? id}
                    </button>
                    <span className="adm-chip-muted">{text(scenario.status) ?? 'draft'}</span>
                  </li>
                )
              })}
            </ul>
            {scenarios.data?.truncated && (
              <p className="adm-field-help" role="status">
                The scenario list is truncated.
              </p>
            )}
          </section>

          <section className="adm-panel glass-panel">
            <h3 className="adm-panel-title">Schedules, occurrences, and approvals</h3>
            <p className="adm-field-help">
              Selected scenario: {scenarioId || 'Choose a scenario above'}
            </p>
            <textarea
              className="adm-json-input"
              aria-label="Schedule JSON"
              rows={6}
              value={scheduleDraft}
              onChange={(event) => setScheduleDraft(event.target.value)}
            />
            <div className="adm-panel-actions">
              <button
                className="btn btn-primary btn-sm"
                type="button"
                disabled={!scenarioId || createSchedule.isPending}
                onClick={() => createSchedule.mutate()}
              >
                Create native schedule
              </button>
            </div>
            {(createSchedule.data || createSchedule.error) && (
              <pre className="adm-json-input">
                {createSchedule.error instanceof Error
                  ? createSchedule.error.message
                  : JSON.stringify(createSchedule.data?.data, null, 2)}
              </pre>
            )}
            {schedules.isError && (
              <ProblemCard
                title="Schedules unavailable"
                message={schedules.error.message}
                onRetry={() => void schedules.refetch()}
              />
            )}
            <ul className="data-table">
              {schedules.data?.items.map((schedule) => {
                const id = text(schedule.id) ?? 'unknown'
                return (
                  <li key={id}>
                    <button
                      className="btn btn-ghost btn-sm"
                      type="button"
                      onClick={() => {
                        setScheduleId(id)
                        setApprovalId('')
                      }}
                    >
                      {text(schedule.name) ?? id}
                    </button>
                    <span className="adm-chip-muted">{text(schedule.status) ?? 'unknown'}</span>
                  </li>
                )
              })}
            </ul>
            {schedules.data?.truncated && (
              <p className="adm-field-help" role="status">
                The schedule list is truncated.
              </p>
            )}
            <p className="adm-field-help">
              Selected schedule: {scheduleId || 'Choose a schedule above'}
            </p>
            {scheduleId && (
              <JsonPanel
                title="Upcoming schedule occurrences"
                value={occurrences.data ?? []}
                error={occurrences.error}
                pending={occurrences.isPending}
                onRetry={() => void occurrences.refetch()}
              />
            )}
            <div className="adm-panel-actions">
              <input
                aria-label="Provider schedule approval ID"
                placeholder="Provider schedule approval ID"
                value={approvalId}
                onChange={(event) => setApprovalId(event.target.value)}
              />
              <button
                className="btn btn-secondary btn-sm"
                type="button"
                disabled={
                  !scenarioId || !scheduleId || !approvalId || decideScheduleApproval.isPending
                }
                onClick={() => decideScheduleApproval.mutate(true)}
              >
                Approve
              </button>
              <button
                className="btn btn-danger btn-sm"
                type="button"
                disabled={
                  !scenarioId || !scheduleId || !approvalId || decideScheduleApproval.isPending
                }
                onClick={() => decideScheduleApproval.mutate(false)}
              >
                Reject
              </button>
            </div>
            {(decideScheduleApproval.data || decideScheduleApproval.error) && (
              <pre className="adm-json-input">
                {decideScheduleApproval.error instanceof Error
                  ? decideScheduleApproval.error.message
                  : JSON.stringify(decideScheduleApproval.data?.data, null, 2)}
              </pre>
            )}
          </section>
          </>}

          {workspaceView === 'executions' && <section className="adm-panel glass-panel" aria-label="Load executions">
            <h3 className="adm-panel-title">Load executions and analysis</h3>
            <p className="adm-field-help">Each execution keeps its provider status and Orchestration analysis together.</p>
            {nativeRuns.isError && (
              <ProblemCard
                title="Native runs unavailable"
                message={nativeRuns.error.message}
                onRetry={() => void nativeRuns.refetch()}
              />
            )}
            <div className="adm-card-grid">
              {(nativeRuns.data?.items ?? []).map((run) => {
                const id = text(run.id) ?? 'unknown'
                const mirrored = mirroredRuns.data?.find((candidate) => candidate.id === id)
                const consoleUrl = executionLink(
                  run.console_url,
                  `/apex-load?connection=${encodeURIComponent(connectionId)}&run=${encodeURIComponent(id)}`,
                )
                return (
                  <article className="adm-card glass-panel" key={id}>
                    <strong>{text(run.name) ?? id}</strong>
                    <span>{text(run.status) ?? 'unknown'}</span>
                    <span className="adm-field-help">
                      {mirrored
                        ? ({
                            pending: 'Waiting for analysis',
                            launching: 'Starting analysis',
                            launched: 'Analysis created',
                            retry: 'Retry scheduled',
                            stale: 'Connection changed',
                          }[mirrored.pipeline_status] ?? mirrored.pipeline_status)
                        : 'Waiting for analysis'}
                    </span>
                    <button
                      className="btn btn-secondary btn-sm"
                      type="button"
                      onClick={() => {
                        setSelectedRun(id)
                        setSearchParams({ connection: connectionId, run: id })
                        setEvidenceSignature('')
                      }}
                    >
                      Live metrics
                    </button>
                    {consoleUrl && (
                      <a href={consoleUrl} target="_blank" rel="noreferrer">
                        Open in APEX Load
                      </a>
                    )}
                    {mirrored?.pipeline_thread_id ? (
                      <Link to={`/runs/${encodeURIComponent(mirrored.pipeline_thread_id)}`}>
                        Open analysis
                      </Link>
                    ) : null}
                  </article>
                )
              })}
            </div>
            {nativeRuns.data?.truncated && (
              <p className="adm-field-help" role="status">
                Native run history is truncated; use mirrored history or refine the provider view.
              </p>
            )}
            <div className="adm-inline-status" aria-live="polite">
              <span>Completed Load runs are collected automatically.</span>
              <button
                className="btn btn-secondary btn-sm"
                onClick={() => void mirroredRuns.refetch()}
              >
                Refresh analysis status
              </button>
            </div>
            {mirroredRuns.isError && (
              <ProblemCard
                title="Analysis status unavailable"
                message={mirroredRuns.error.message}
                onRetry={() => void mirroredRuns.refetch()}
              />
            )}
            {selectedRun && (
              <JsonPanel
                title={`Live metrics: ${selectedRun}`}
                value={runMetrics.data ?? {}}
                error={runMetrics.error}
                pending={runMetrics.isPending}
                onRetry={() => void runMetrics.refetch()}
              />
            )}
            {selectedRun && (
              <div className="adm-panel-actions">
                <input
                  aria-label="Error evidence signature"
                  placeholder="Error signature for root-cause evidence"
                  value={evidenceSignature}
                  onChange={(event) => setEvidenceSignature(event.target.value)}
                />
              </div>
            )}
            {selectedRun && evidenceSignature && (
              <JsonPanel
                title={`Collected error evidence: ${selectedRun}`}
                value={runEvidence.data ?? {}}
                error={runEvidence.error}
                pending={runEvidence.isPending}
                onRetry={() => void runEvidence.refetch()}
              />
            )}
          </section>}
        </>
      )}
    </div>
  )
}
