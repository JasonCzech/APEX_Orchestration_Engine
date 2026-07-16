/**
 * Step 1 — Scope: run intent (title + request) and the catalog scope chain
 * project -> application -> environment. Changing a parent resets children.
 */
import { useApplications, useEnvironments } from '@/api/hooks/useCatalog'
import { useOptionalConsumer } from '@/auth/AuthProvider'

import type { StepProps } from '../NewRunWizard'

export function ScopeStep({ draft, onChange }: StepProps) {
  const consumer = useOptionalConsumer()
  const scoped = consumer !== null && consumer !== undefined && consumer.scopes.length > 0
  const scopedProjects = Array.from(
    new Set(
      (consumer?.scopes ?? [])
        .map((scope) => scope.project_id.trim())
        .filter((projectId) => projectId.length > 0),
    ),
  ).sort()
  const selectedProjectScopes = (consumer?.scopes ?? []).filter(
    (scope) => scope.project_id === draft.scope.project_id,
  )
  const hasProjectWideScope = selectedProjectScopes.some((scope) => !scope.app_id)
  const scopedAppIds = Array.from(
    new Set(
      selectedProjectScopes
        .map((scope) => scope.app_id?.trim())
        .filter((appId): appId is string => Boolean(appId)),
    ),
  ).sort()
  const requiresApplication = scoped && selectedProjectScopes.length > 0 && !hasProjectWideScope
  const projectAuthorized =
    !scoped || scopedProjects.includes(draft.scope.project_id)
  const applicationAuthorized =
    !scoped ||
    (projectAuthorized &&
      (hasProjectWideScope ||
        (draft.scope.app_id !== null && scopedAppIds.includes(draft.scope.app_id))))
  const applications = useApplications(projectAuthorized ? draft.scope.project_id : undefined)
  const environments = useEnvironments(
    applicationAuthorized ? draft.scope.app_id : null,
  )

  const applicationOptions = hasProjectWideScope || !scoped
    ? (applications.data ?? [])
    : scopedAppIds.map((appId) => applications.data?.find((app) => app.id === appId) ?? {
        id: appId,
        name: appId,
      })
  const selectedApp = applications.data?.find((app) => app.id === draft.scope.app_id)
  const selectedEnv = environments.data?.find((env) => env.id === draft.scope.environment_id)

  function changeProject(projectId: string) {
    const nextScopes = (consumer?.scopes ?? []).filter((scope) => scope.project_id === projectId)
    const nextHasProjectWideScope = nextScopes.some((scope) => !scope.app_id)
    const nextScopedApps = Array.from(
      new Set(
        nextScopes
          .map((scope) => scope.app_id?.trim())
          .filter((appId): appId is string => Boolean(appId)),
      ),
    )
    const appId =
      scoped && !nextHasProjectWideScope && nextScopedApps.length === 1
        ? nextScopedApps[0] ?? null
        : null
    onChange((prev) => ({
      ...prev,
      scope: { project_id: projectId, app_id: appId, environment_id: null },
      // These selections are project-owned; never carry them into a
      // different project where their ids/keys may resolve differently.
      document_ids: [],
      work_items: [],
      context_summary_ids: [],
      prompt_overrides: {},
      prompt_override_removals: [],
      config: {
        ...prev.config,
        golden_config_id: null,
        golden_configurable: null,
      },
    }))
  }

  return (
    <section className="wizard-step" aria-label="Scope">
      <div className="wizard-field">
        <label className="wizard-label" htmlFor="wizard-title">
          Title
        </label>
        <input
          id="wizard-title"
          className="field-input"
          value={draft.title}
          placeholder="Checkout latency soak"
          onChange={(event) =>
            onChange((prev) => ({ ...prev, title: event.target.value }))
          }
        />
      </div>

      <div className="wizard-field">
        <label className="wizard-label" htmlFor="wizard-request">
          Request
        </label>
        <textarea
          id="wizard-request"
          className="field-input wizard-textarea"
          rows={4}
          value={draft.request}
          placeholder="What should this run test, and what does success look like?"
          onChange={(event) =>
            onChange((prev) => ({ ...prev, request: event.target.value }))
          }
        />
      </div>

      <div className="wizard-field">
        <label className="wizard-label" htmlFor="wizard-project">
          Project
        </label>
        {scoped ? (
          <select
            id="wizard-project"
            className="field-select"
            value={scopedProjects.includes(draft.scope.project_id) ? draft.scope.project_id : ''}
            onChange={(event) => changeProject(event.target.value)}
          >
            <option value="" disabled>
              Select an authorized project…
            </option>
            {scopedProjects.map((projectId) => (
              <option key={projectId} value={projectId}>
                {projectId}
              </option>
            ))}
          </select>
        ) : (
          <input
            id="wizard-project"
            className="field-input"
            value={draft.scope.project_id}
            onChange={(event) => changeProject(event.target.value)}
          />
        )}
        {scoped && scopedProjects.length === 0 && (
          <p className="wizard-caption wizard-caption--danger">
            No valid project scopes are assigned to this consumer.
          </p>
        )}
      </div>

      <div className="wizard-field">
        <label className="wizard-label" htmlFor="wizard-application">
          Application
        </label>
        <select
          id="wizard-application"
          className="field-select"
          value={
            applicationOptions.some((app) => app.id === draft.scope.app_id)
              ? draft.scope.app_id ?? ''
              : ''
          }
          onChange={(event) =>
            onChange((prev) => {
              const nextApp = event.target.value || null
              const prompt_overrides = Object.fromEntries(
                Object.entries(prev.prompt_overrides).filter(
                  ([key]) => !key.startsWith('application/') || key === `application/${nextApp}`,
                ),
              )
              return {
                ...prev,
                prompt_overrides,
                document_ids: [],
                scope: { ...prev.scope, app_id: nextApp, environment_id: null },
              }
            })
          }
        >
          <option value="" disabled={requiresApplication}>
            {requiresApplication ? 'Select an authorized application…' : '— none —'}
          </option>
          {applicationOptions.map((app) => (
            <option key={app.id} value={app.id}>
              {app.name}
            </option>
          ))}
        </select>
        {requiresApplication && !draft.scope.app_id && (
          <p className="wizard-caption">An application is required by your assigned scope.</p>
        )}
        {applications.isError && (
          <p className="wizard-caption wizard-caption--danger">Applications failed to load</p>
        )}
        {selectedApp?.description && <p className="wizard-caption">{selectedApp.description}</p>}
      </div>

      <div className="wizard-field">
        <label className="wizard-label" htmlFor="wizard-environment">
          Environment
        </label>
        <select
          id="wizard-environment"
          className="field-select"
          value={draft.scope.environment_id ?? ''}
          disabled={!draft.scope.app_id}
          onChange={(event) =>
            onChange((prev) => ({
              ...prev,
              scope: { ...prev.scope, environment_id: event.target.value || null },
            }))
          }
        >
          <option value="">— none —</option>
          {(environments.data ?? []).map((env) => (
            <option key={env.id} value={env.id}>
              {env.name}
            </option>
          ))}
        </select>
        {environments.isError && (
          <p className="wizard-caption wizard-caption--danger">Environments failed to load</p>
        )}
        {selectedEnv && (
          <p className="wizard-caption">
            {selectedEnv.kind ?? 'environment'}
            {selectedEnv.base_url ? ` · ${selectedEnv.base_url}` : ''}
          </p>
        )}
      </div>
    </section>
  )
}
