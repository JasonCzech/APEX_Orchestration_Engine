import { useDocumentsList } from '@/api/hooks/useDocuments'
import { useEnvironment } from '@/api/hooks/useEnvironments'
import { useExecutionReadiness } from '@/api/hooks/useExecutionReadiness'
import { assessWizardReadiness, isRecord, selectedPhases, type WizardDraft } from './wizardState'

/** The footer and launch preview must consult the same live target evidence. */
export function useWizardReadiness(draft: WizardDraft, maxContextPackets?: number) {
  const documents = useDocumentsList(draft.scope.project_id.trim() || undefined, undefined, draft.scope.app_id)
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
    project_id: project,
    ...(draft.scope.app_id ? { app_id: draft.scope.app_id } : {}),
    engine: draft.config.engine,
    ...(typeof connection === 'string' ? { connection_id: connection } : {}),
    ...(typeof artifactConnection === 'string' ? { artifact_connection_id: artifactConnection } : {}),
    phases,
  }, verifiesExecution)
  return {
    documents,
    environment,
    execution,
    executionVerificationEnabled: verifiesExecution,
    readiness: assessWizardReadiness(draft, maxContextPackets, {
      ids: documents.isError ? undefined : documents.data?.map(doc => doc.id),
      failed: documents.isError,
    }, {
      environment: environment.isError ? undefined : environment.data,
      failed: environment.isError,
    }, verifiesExecution ? {
      configuration: execution.isError ? undefined : execution.data,
      failed: execution.isError,
      error: execution.error?.message,
    } : undefined),
  }
}
