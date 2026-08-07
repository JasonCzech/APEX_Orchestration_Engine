import { useEffect, useMemo, useState } from 'react'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { ProblemCard } from '@/components/ProblemCard'

import '../admin/admin.css'

type NativeObject = Record<string, unknown>

interface NativeConnection {
  id: string
  name: string
  project_id: string
}

function object(value: unknown): NativeObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as NativeObject)
    : {}
}

function rows(value: unknown): NativeObject[] {
  const items = object(value).items
  return Array.isArray(items) ? items.map(object) : []
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null
}

function nativeError(response: Response, error: unknown, label: string): ApiError {
  return new ApiError(response.status, errorMessageOf(error, `${label} failed (${response.status})`), error)
}

async function fetchConnections(): Promise<NativeConnection[]> {
  const { data, error, response } = await getApexClient().GET('/v1/apex-load/connections')
  if (!response.ok || !data) throw nativeError(response, error, 'APEX Load connections')
  const items = object(data).items
  if (!Array.isArray(items)) return []
  return items.flatMap((item) => {
    const value = object(item)
    const id = text(value.id)
    const name = text(value.name)
    const projectId = text(value.project_id)
    return id && name && projectId ? [{ id, name, project_id: projectId }] : []
  })
}

async function fetchNativeObject(
  connectionId: string,
  resource: 'capabilities' | 'schedule-health' | 'calendar',
): Promise<NativeObject> {
  const path = `/v1/apex-load/connections/{connection_id}/${resource}` as const
  const { data, error, response } = await getApexClient().GET(path, {
    params: { path: { connection_id: connectionId } },
  })
  if (!response.ok || !data) throw nativeError(response, error, `APEX Load ${resource}`)
  return object(data)
}

async function fetchNativeRows(
  connectionId: string,
  resource: 'scenarios' | 'runs' | 'mirrored-runs',
): Promise<NativeObject[]> {
  const client = getApexClient()
  const result =
    resource === 'runs'
      ? await client.GET('/v1/apex-load/connections/{connection_id}/runs', {
          params: { path: { connection_id: connectionId } },
        })
      : resource === 'mirrored-runs'
        ? await client.GET('/v1/apex-load/connections/{connection_id}/mirrored-runs', {
            params: { path: { connection_id: connectionId }, query: { limit: 25, offset: 0 } },
          })
        : await client.GET('/v1/apex-load/connections/{connection_id}/scenarios', {
            params: { path: { connection_id: connectionId } },
          })
  if (!result.response.ok || !result.data) {
    throw nativeError(result.response, result.error, `APEX Load ${resource}`)
  }
  return rows(result.data)
}

async function fetchSchedules(connectionId: string, scenarioId: string): Promise<NativeObject[]> {
  const { data, error, response } = await getApexClient().GET(
    '/v1/apex-load/connections/{connection_id}/scenarios/{scenario_id}/schedules',
    { params: { path: { connection_id: connectionId, scenario_id: scenarioId } } },
  )
  if (!response.ok || !data) throw nativeError(response, error, 'APEX Load schedules')
  return rows(data)
}

function parseDraft(value: string): NativeObject {
  const parsed: unknown = JSON.parse(value)
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Draft JSON must be an object.')
  }
  return parsed as NativeObject
}

function JsonPanel({ title, value }: { title: string; value: unknown }) {
  return (
    <section className="adm-panel glass-panel">
      <h3 className="adm-panel-title">{title}</h3>
      <pre className="adm-json-input">{JSON.stringify(value, null, 2)}</pre>
    </section>
  )
}

export function ApexLoadWorkspacePage() {
  const queryClient = useQueryClient()
  const connections = useQuery({ queryKey: ['apex-load', 'connections'], queryFn: fetchConnections })
  const [connectionId, setConnectionId] = useState('')
  const [scenarioId, setScenarioId] = useState('')
  const [scheduleId, setScheduleId] = useState('')
  const [selectedRun, setSelectedRun] = useState('')
  const [approvalId, setApprovalId] = useState('')
  const [evidenceSignature, setEvidenceSignature] = useState('')
  const [scenarioDraft, setScenarioDraft] = useState('{\n  "name": "New scenario"\n}')
  const [scheduleDraft, setScheduleDraft] = useState('{\n  "name": "Scheduled run"\n}')

  useEffect(() => {
    if (!connectionId && connections.data?.[0]) setConnectionId(connections.data[0].id)
  }, [connectionId, connections.data])

  const capabilities = useQuery({
    queryKey: ['apex-load', connectionId, 'capabilities'],
    queryFn: () => fetchNativeObject(connectionId, 'capabilities'),
    enabled: Boolean(connectionId),
  })
  const scenarios = useQuery({
    queryKey: ['apex-load', connectionId, 'scenarios'],
    queryFn: () => fetchNativeRows(connectionId, 'scenarios'),
    enabled: Boolean(connectionId),
  })
  const nativeRuns = useQuery({
    queryKey: ['apex-load', connectionId, 'runs'],
    queryFn: () => fetchNativeRows(connectionId, 'runs'),
    enabled: Boolean(connectionId),
    refetchInterval: 10_000,
  })
  const mirroredRuns = useQuery({
    queryKey: ['apex-load', connectionId, 'mirrored-runs'],
    queryFn: () => fetchNativeRows(connectionId, 'mirrored-runs'),
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
        { params: { path: { connection_id: connectionId, scenario_id: scenarioId, schedule_id: scheduleId } } },
      )
      if (!response.ok || !data) throw nativeError(response, error, 'APEX Load schedule occurrences')
      return rows(data)
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
      return object(data)
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
      return object(data)
    },
    enabled: Boolean(connectionId && selectedRun && evidenceSignature),
  })

  useEffect(() => {
    if (!scenarioId) {
      const first = scenarios.data?.map((scenario) => text(scenario.id)).find(Boolean)
      if (first) setScenarioId(first)
    }
  }, [scenarioId, scenarios.data])

  useEffect(() => {
    if (!scheduleId) {
      const first = schedules.data?.map((schedule) => text(schedule.id)).find(Boolean)
      if (first) setScheduleId(first)
    }
  }, [scheduleId, schedules.data])

  const refreshConnection = () => {
    void queryClient.invalidateQueries({ queryKey: ['apex-load', connectionId] })
  }
  const validateScenario = useMutation({
    mutationFn: async () => {
      const { data, error, response } = await getApexClient().POST(
        '/v1/apex-load/connections/{connection_id}/scenarios/validation',
        { params: { path: { connection_id: connectionId } }, body: { payload: parseDraft(scenarioDraft) } },
      )
      if (!response.ok || !data) throw nativeError(response, error, 'APEX Load scenario validation')
      return object(data)
    },
  })
  const sizeScenario = useMutation({
    mutationFn: async () => {
      const { data, error, response } = await getApexClient().POST(
        '/v1/apex-load/connections/{connection_id}/scenarios/sizing',
        { params: { path: { connection_id: connectionId } }, body: { payload: parseDraft(scenarioDraft) } },
      )
      if (!response.ok || !data) throw nativeError(response, error, 'APEX Load scenario sizing')
      return object(data)
    },
  })
  const createScenario = useMutation({
    mutationFn: async () => {
      const { data, error, response } = await getApexClient().POST(
        '/v1/apex-load/connections/{connection_id}/scenarios',
        { params: { path: { connection_id: connectionId } }, body: { payload: parseDraft(scenarioDraft) } },
      )
      if (!response.ok || !data) throw nativeError(response, error, 'APEX Load scenario create')
      return object(data)
    },
    onSuccess: refreshConnection,
  })
  const createSchedule = useMutation({
    mutationFn: async () => {
      const { data, error, response } = await getApexClient().POST(
        '/v1/apex-load/connections/{connection_id}/scenarios/{scenario_id}/schedules',
        {
          params: { path: { connection_id: connectionId, scenario_id: scenarioId } },
          body: { payload: parseDraft(scheduleDraft) },
        },
      )
      if (!response.ok || !data) throw nativeError(response, error, 'APEX Load schedule create')
      return object(data)
    },
    onSuccess: refreshConnection,
  })
  const decideScheduleApproval = useMutation({
    mutationFn: async (approved: boolean) => {
      if (!scenarioId || !scheduleId || !approvalId) {
        throw new Error('Choose a scenario, schedule, and approval id first.')
      }
      const { data, error, response } = await getApexClient().POST(
        '/v1/apex-load/connections/{connection_id}/scenarios/{scenario_id}/schedules/{schedule_id}/approvals/{approval_id}',
        {
          params: {
            path: {
              connection_id: connectionId,
              scenario_id: scenarioId,
              schedule_id: scheduleId,
              approval_id: approvalId,
            },
          },
          body: { approved },
        },
      )
      if (!response.ok || !data) throw nativeError(response, error, 'APEX Load schedule approval')
      return object(data)
    },
    onSuccess: refreshConnection,
  })

  const activeConnection = useMemo(
    () => connections.data?.find((connection) => connection.id === connectionId),
    [connectionId, connections.data],
  )

  if (connections.isPending) return <div className="dash-empty">Loading APEX Load connections…</div>
  if (connections.isError) {
    return <ProblemCard title="APEX Load unavailable" message={connections.error.message} onRetry={() => void connections.refetch()} />
  }
  if (!connections.data?.length) {
    return <div className="dash-empty">No enabled, project-scoped APEX Load native-control connection is available.</div>
  }

  return (
    <div className="adm-page" data-testid="apex-load-workspace">
      <header className="adm-toolbar glass-panel">
        <div>
          <h2 className="adm-page-title">APEX Load Control Plane</h2>
          <span className="adm-card-time">Native schedules remain the workload-launch authority.</span>
        </div>
        <label className="adm-field">
          <span className="adm-field-label">Connection</span>
          <select
            value={connectionId}
            onChange={(event) => {
              setConnectionId(event.target.value)
              setScenarioId('')
              setScheduleId('')
              setSelectedRun('')
            }}
          >
            {connections.data.map((connection) => (
              <option key={connection.id} value={connection.id}>
                {connection.name} · {connection.project_id}
              </option>
            ))}
          </select>
        </label>
      </header>

      {activeConnection && <JsonPanel title="Load-model templates and capabilities" value={capabilities.data ?? {}} />}
      <JsonPanel title="Schedule health" value={scheduleHealth.data ?? {}} />
      <JsonPanel title="Native schedule calendar" value={calendar.data ?? {}} />

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
          <button className="btn btn-secondary btn-sm" type="button" onClick={() => validateScenario.mutate()}>
            Validate scenario
          </button>
          <button className="btn btn-secondary btn-sm" type="button" onClick={() => sizeScenario.mutate()}>
            Estimate capacity
          </button>
          <button className="btn btn-primary btn-sm" type="button" onClick={() => createScenario.mutate()}>
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
        <ul className="data-table">
          {scenarios.data?.map((scenario) => {
            const id = text(scenario.id) ?? 'unknown'
            return (
              <li key={id}>
                <button
                  className="btn btn-ghost btn-sm"
                  type="button"
                  onClick={() => {
                    setScenarioId(id)
                    setScheduleId('')
                  }}
                >
                  {text(scenario.name) ?? id}
                </button>
                <span className="adm-chip-muted">{text(scenario.status) ?? 'draft'}</span>
              </li>
            )
          })}
        </ul>
      </section>

      <section className="adm-panel glass-panel">
        <h3 className="adm-panel-title">Schedules, occurrences, and approvals</h3>
        <p className="adm-field-help">Selected scenario: {scenarioId || 'Choose a scenario above'}</p>
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
            disabled={!scenarioId}
            onClick={() => createSchedule.mutate()}
          >
            Create native schedule
          </button>
        </div>
        <ul className="data-table">
          {schedules.data?.map((schedule) => {
            const id = text(schedule.id) ?? 'unknown'
            return (
              <li key={id}>
                <button className="btn btn-ghost btn-sm" type="button" onClick={() => setScheduleId(id)}>
                  {text(schedule.name) ?? id}
                </button>
                <span className="adm-chip-muted">{text(schedule.status) ?? 'unknown'}</span>
              </li>
            )
          })}
        </ul>
        <p className="adm-field-help">Selected schedule: {scheduleId || 'Choose a schedule above'}</p>
        {scheduleId && <JsonPanel title="Upcoming schedule occurrences" value={occurrences.data ?? []} />}
        <div className="adm-panel-actions">
          <input
            aria-label="Schedule approval id"
            placeholder="Schedule approval id"
            value={approvalId}
            onChange={(event) => setApprovalId(event.target.value)}
          />
          <button
            className="btn btn-secondary btn-sm"
            type="button"
            disabled={!scenarioId || !scheduleId || !approvalId}
            onClick={() => decideScheduleApproval.mutate(true)}
          >
            Approve
          </button>
          <button
            className="btn btn-danger btn-sm"
            type="button"
            disabled={!scenarioId || !scheduleId || !approvalId}
            onClick={() => decideScheduleApproval.mutate(false)}
          >
            Reject
          </button>
        </div>
        {(decideScheduleApproval.data || decideScheduleApproval.error) && (
          <pre className="adm-json-input">
            {decideScheduleApproval.error instanceof Error
              ? decideScheduleApproval.error.message
              : JSON.stringify(decideScheduleApproval.data, null, 2)}
          </pre>
        )}
      </section>

      <section className="adm-panel glass-panel">
        <h3 className="adm-panel-title">Native runs and reporting pipelines</h3>
        <div className="adm-card-grid">
          {(nativeRuns.data ?? []).map((run) => {
            const id = text(run.id) ?? 'unknown'
            const consoleUrl = text(run.console_url)
            return (
              <article className="adm-card glass-panel" key={id}>
                <strong>{text(run.name) ?? id}</strong>
                <span>{text(run.status) ?? 'unknown'}</span>
                <button
                  className="btn btn-secondary btn-sm"
                  type="button"
                  onClick={() => {
                    setSelectedRun(id)
                    setEvidenceSignature('')
                  }}
                >
                  Live metrics
                </button>
                {consoleUrl && <a href={consoleUrl} target="_blank" rel="noreferrer">Open in APEX Load</a>}
              </article>
            )
          })}
        </div>
        <JsonPanel title="Mirrored runs and exactly-once launch state" value={mirroredRuns.data ?? []} />
        {selectedRun && <JsonPanel title={`Live metrics: ${selectedRun}`} value={runMetrics.data ?? {}} />}
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
          <JsonPanel title={`Collected error evidence: ${selectedRun}`} value={runEvidence.data ?? {}} />
        )}
      </section>
    </div>
  )
}
