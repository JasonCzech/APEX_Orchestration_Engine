import { useQuery } from '@tanstack/react-query'
import { getApexClient } from '@/api/apexClient'
import type { StepProps } from './NewRunWizard'

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

/** Catalog choices are scoped by the facade; launch revalidates their ownership. */
export function ReleaseMappingPicker({ draft, onChange, disabled }: StepProps) {
  const project = draft.scope.test_scope?.participants.find(p => p.id === draft.scope.test_scope?.traffic_entry_participant_id)?.project_id ?? draft.scope.project_id
  const inherited = draft.config.golden_configurable ?? {}
  const connections = record(inherited.connections)
  const connectionId = typeof connections.execution_engine === 'string' ? connections.execution_engine : ''
  const loadTest = record(inherited.load_test)
  const catalog = useQuery({
    queryKey: ['release-mapping-connections', project],
    queryFn: async () => {
      const result = await getApexClient().GET('/v1/apex-load/connections')
      if (!result.response.ok || !result.data) throw new Error('Load connections could not be loaded.')
      return result.data.items.filter(item => item.project_id === project)
    },
  })
  const eligible = catalog.data?.some(item => item.id === connectionId) ?? false
  const mapping = useQuery({
    queryKey: ['release-mapping', project, connectionId],
    enabled: eligible,
    queryFn: async () => {
      const result = await getApexClient().GET('/v1/apex-load/connections/{connection_id}/capabilities', {
        params: { path: { connection_id: connectionId } },
      })
      if (!result.response.ok || !result.data) throw new Error('Release mapping unavailable. Check the connection mapping and retry.')
      return result.data.release_mapping ?? null
    },
  })
  function select(connection: string, release?: string) {
    onChange(previous => {
      const bundle = previous.config.golden_configurable ?? {}
      const nextLoad = { ...record(bundle.load_test) }
      delete nextLoad.release_id
      delete nextLoad.mapping_revision
      if (release && eligible && !mapping.isError && !mapping.isFetching && mapping.data?.releases.some(item => item.id === release)) {
        nextLoad.release_id = release
        nextLoad.mapping_revision = mapping.data.revision
      }
      const nextConnections = { ...record(bundle.connections) }
      if (connection) nextConnections.execution_engine = connection
      else delete nextConnections.execution_engine
      return { ...previous, config: { ...previous.config, golden_configurable: {
        ...bundle, connections: nextConnections, load_test: nextLoad,
      } } }
    })
  }
  const selectedRelease = eligible && mapping.data && mapping.data.revision === loadTest.mapping_revision && mapping.data.releases.some(item => item.id === loadTest.release_id)
    ? String(loadTest.release_id) : ''
  return <div className="wizard-field">
    <label className="wizard-label" htmlFor="release-connection">Load connection</label>
    <select className="field-select" id="release-connection" value={connectionId} disabled={disabled || catalog.isFetching} onChange={event => select(event.target.value)}>
      <option value="">Use project default connection</option>
      {connectionId && !eligible && <option value={connectionId}>Unavailable connection · {connectionId}</option>}
      {catalog.data?.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
    </select>
    {catalog.isError && <p role="alert">{catalog.error.message} <button type="button" onClick={() => void catalog.refetch()}>Retry connections</button></p>}
    {connectionId && catalog.isSuccess && !eligible && <p role="alert">The saved Load connection is unavailable in this project. Select an available connection or explicitly choose the project default.</p>}
    {eligible && mapping.isError && <p role="alert">{mapping.error.message} <button type="button" onClick={() => void mapping.refetch()}>Retry releases</button></p>}
    {eligible && mapping.isSuccess && !mapping.data && <p className="wizard-caption">No release mapping is configured for this connection.</p>}
    {eligible && mapping.isFetching && <p role="status">Loading releases…</p>}
    {eligible && mapping.data && <>
      <label className="wizard-label" htmlFor="mapped-release">Release</label>
      <select className="field-select" id="mapped-release" disabled={disabled || mapping.isFetching || mapping.isError} value={selectedRelease} onChange={event => select(connectionId, event.target.value)}>
        <option value="">Select a release</option>
        {mapping.data.releases.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}
      </select>
      <p className="wizard-caption">Application {mapping.data.application_id} · mapping {mapping.data.revision}. This identity is recorded with the run.</p>
      {!selectedRelease && <p role="status">Select an active release before launching this mapped connection.</p>}
    </>}
  </div>
}
