/**
 * Step 1 — Scope: run intent (title + request) and the catalog scope chain
 * project -> application -> environment. Changing a parent resets children.
 */
import { useApplications, useEnvironments } from '@/api/hooks/useCatalog'

import type { StepProps } from '../NewRunWizard'

export function ScopeStep({ draft, onChange }: StepProps) {
  const applications = useApplications(draft.scope.project_id)
  const environments = useEnvironments(draft.scope.app_id)

  const selectedApp = applications.data?.find((app) => app.id === draft.scope.app_id)
  const selectedEnv = environments.data?.find((env) => env.id === draft.scope.environment_id)

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
        <input
          id="wizard-project"
          className="field-input"
          value={draft.scope.project_id}
          onChange={(event) =>
            onChange((prev) => ({
              ...prev,
              scope: { project_id: event.target.value, app_id: null, environment_id: null },
              // These selections are project-owned; never carry them into a
              // different project where their ids/keys may resolve differently.
              document_ids: [],
              work_item_keys: [],
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
        />
      </div>

      <div className="wizard-field">
        <label className="wizard-label" htmlFor="wizard-application">
          Application
        </label>
        <select
          id="wizard-application"
          className="field-select"
          value={draft.scope.app_id ?? ''}
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
          <option value="">— none —</option>
          {(applications.data ?? []).map((app) => (
            <option key={app.id} value={app.id}>
              {app.name}
            </option>
          ))}
        </select>
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
