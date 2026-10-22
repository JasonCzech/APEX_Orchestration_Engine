import { Link } from 'react-router'
import { useEnvironment } from '@/api/hooks/useEnvironments'
import { ApplicationLabel } from './ApplicationLabel'

/** Current entity scope, always derived from its persisted/draft identity. */
export function ScopeContext({ project, application, environment }: {
  project?: string | null; application?: string | null; environment?: string | null
}) {
  const env = useEnvironment(environment ?? undefined)
  const loadParams = new URLSearchParams()
  if (project) loadParams.set('project', project)
  return <nav className="scope-context" aria-label="Current run scope">
    <span><strong>Project</strong> {project || 'Not selected'}</span>
    <span><strong>Application</strong> <ApplicationLabel project={project} application={application} empty="Not selected" /></span>
    {environment && <span title={environment}><strong>Environment</strong> {env.data?.name ?? environment}</span>}
    {project && <Link to={`/apex-load?${loadParams}`}>Load integration for this project</Link>}
  </nav>
}
