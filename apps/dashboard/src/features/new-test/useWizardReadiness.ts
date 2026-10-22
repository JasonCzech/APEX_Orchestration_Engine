import { useQueries } from '@tanstack/react-query'
import { fetchApplications, fetchEnvironments } from '@/api/hooks/useCatalog'
import { useOptionalAuth } from '@/auth/AuthProvider'
import { testScopeIssues } from './testScope'
import { useAnalysisConnections } from '@/api/hooks/useAnalysisConnections'
import { useTestScopeDocuments } from '@/api/hooks/useDocuments'
import { useEnvironment } from '@/api/hooks/useEnvironments'
import { useExecutionReadiness } from '@/api/hooks/useExecutionReadiness'
import { assessWizardReadiness, isRecord, selectedPhases, type WizardDraft } from './wizardState'

/** The footer and launch preview must consult the same live target evidence. */
export function useWizardReadiness(draft: WizardDraft, maxContextPackets?: number) {
  const auth = useOptionalAuth()
  const maxParticipants = auth?.state.status === 'authenticated' ? auth.state.systemInfo.limits.max_test_participants : 32
  const participants = draft.scope.test_scope?.participants ?? []
  const participantQueries = useQueries({ queries: participants.map(p => ({
    queryKey: ['test-participant-catalog', p.project_id, p.application_id, p.environment_id],
    enabled: Boolean(p.project_id && p.application_id && p.environment_id),
    retry: false,
    queryFn: async ({ signal }: { signal: AbortSignal }) => {
      const [apps, envs] = await Promise.all([fetchApplications(p.project_id, signal), fetchEnvironments(p.application_id, signal)])
      return apps.some(app => app.id === p.application_id && app.project_id === p.project_id && !app.archived_at) && envs.some(env => env.id === p.environment_id && env.application_id === p.application_id)
    },
  })) })
  const documents = useTestScopeDocuments(draft.scope)
  const environment = useEnvironment(draft.scope.environment_id ?? undefined)
  const project = draft.scope.project_id.trim()
  const phases = selectedPhases(draft.config)
  const connections = draft.config.golden_configurable?.connections
  const connection = isRecord(connections) ? connections.execution_engine : undefined
  const artifactConnection = isRecord(connections) ? connections.artifact_store : undefined
  const validConnections = connections === undefined || (isRecord(connections) &&
    [connection, artifactConnection].every(value => value === undefined || (typeof value === 'string' && value.trim())))
  const verifiesExecution = Boolean(project) && phases.includes('execution') && !draft.config.unsupported_engine && validConnections
  const execution = useExecutionReadiness({
    test_scope: draft.scope.test_scope,
    project_id: project,
    ...(draft.scope.app_id ? { app_id: draft.scope.app_id } : {}),
    engine: draft.config.engine,
    ...(typeof connection === 'string' ? { connection_id: connection } : {}),
    ...(typeof artifactConnection === 'string' ? { artifact_connection_id: artifactConnection } : {}),
    phases,
  }, verifiesExecution)
  const trafficProject = draft.scope.test_scope?.participants.find(p => p.id === draft.scope.test_scope?.traffic_entry_participant_id)?.project_id ?? project
  const analysisConnections = useAnalysisConnections(project, draft.config.engine !== 'sim')
  const nativeConnections = useAnalysisConnections(trafficProject, draft.config.engine !== 'sim')
  const analysisIssues = (draft.config.analysis?.selections ?? []).flatMap(selection => {
    const choices = selection.tool === 'internal_rca' ? nativeConnections : analysisConnections
    if (choices.isError) return [{ step: 'config' as const, message: 'Analysis connectors could not be verified; retry before launch' }]
    if (!choices.data) return [{ step: 'config' as const, message: 'Verifying analysis connectors before launch' }]
    const match = choices.data.find(c => c.id === selection.connection_id && (selection.tool === 'internal_rca' ? c.kind === 'execution_engine' && c.provider === 'apex_load' : c.kind === 'analysis' && c.provider === selection.tool))
    return match ? [] : [{ step: 'config' as const, message: `Analysis connector ${selection.connection_id || '(not selected)'} is unavailable; choose a compatible connector` }]
  })
  const readiness = assessWizardReadiness(draft, maxContextPackets, {
      ids: documents.isError ? undefined : documents.data?.map(doc => doc.id), failed: documents.isError,
    }, { environment: environment.isError ? undefined : environment.data, failed: environment.isError }, verifiesExecution ? {
      configuration: execution.isError ? undefined : execution.data, failed: execution.isError, error: execution.error?.message,
    } : undefined)
  readiness.planningIssues.push(...analysisIssues)
  if (participants.length > maxParticipants) readiness.planningIssues.push({ step: 'scope', message: testScopeIssues(draft.scope.test_scope, maxParticipants)[0]! })
  participantQueries.forEach((query, index) => {
    const p = participants[index]!
    if (!p.application_id || !p.environment_id || !p.project_id) return
    if (query.isError || query.data === false) readiness.planningIssues.push({ step: 'scope', message: `Participant ${p.application_id} / ${p.environment_id} is unavailable or does not match its project. Re-select it or retry catalog verification.` })
    else if (query.data === undefined) readiness.planningIssues.push({ step: 'scope', message: `Verifying participant ${p.application_id} / ${p.environment_id}` })
  })
  return {
    participantQueries,
    analysisConnections,
    documents,
    environment,
    execution,
    executionVerificationEnabled: verifiesExecution,
    readiness,
  }
}
