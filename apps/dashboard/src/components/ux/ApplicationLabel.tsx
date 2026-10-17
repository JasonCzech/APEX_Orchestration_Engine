import { useApplications } from '@/api/hooks/useCatalog'

/** Resolve the persisted identity through the same catalog on every run surface. */
export function ApplicationLabel({ project, application, empty = 'Not recorded' }: {
  project?: string | null
  application?: string | null
  empty?: string
}) {
  const apps = useApplications(application ? project ?? undefined : undefined)
  const name = apps.data?.find(app => app.id === application)?.name?.trim()
  return <span title={application ?? undefined}>{name || application || empty}</span>
}
