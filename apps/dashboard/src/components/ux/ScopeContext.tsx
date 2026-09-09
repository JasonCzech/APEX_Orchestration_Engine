import { Link } from 'react-router'
import { useApplications, useEnvironments } from '@/api/hooks/useCatalog'

/** Current entity scope, always derived from its persisted/draft identity. */
export function ScopeContext({ project, application, environment }: {
  project?: string | null; application?: string | null; environment?: string | null
}) {
  const apps = useApplications(application ? project ?? undefined : undefined)
  const envs = useEnvironments(environment ? application : undefined)
  const loadParams = new URLSearchParams()
  if (project) loadParams.set('project', project)
  return <nav className="scope-context" aria-label="Current run scope">
    <span><strong>Project</strong> {project || 'Not selected'}</span>
    <span title={application ?? undefined}><strong>Application</strong> {apps.data?.find(app => app.id === application)?.name ?? application ?? 'Not selected'}</span>
    {environment && <span title={environment}><strong>Environment</strong> {envs.data?.find(env => env.id === environment)?.name ?? environment}</span>}
    {project && <Link to={`/apex-load?${loadParams}`}>Load integration for this project</Link>}
  </nav>
}
