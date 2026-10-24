import { useApplicationsIndex, useEnvironmentsIndex } from '@/api/hooks/useEnvironments'
import { useProjectLabels } from '@/api/hooks/useProjectLabels'

import type { FieldNames } from './presetModel'

/** Name lookups for preset summaries (IDs fall back to themselves). */
export function useFieldNames(): FieldNames & { project: (id: string) => string; applicationName: (id: string | null) => string } {
  const applications = useApplicationsIndex({ includeArchived: true })
  const environments = useEnvironmentsIndex()
  const labels = useProjectLabels()
  const application = (id: string) => applications.data?.find(app => app.id === id)?.name
  return {
    application,
    environment: (id: string) => environments.data?.find(env => env.id === id)?.name,
    project: (id: string) => (!labels.isError && labels.data?.find(row => row.project_id === id)?.name) || id,
    applicationName: (id: string | null) => (id ? application(id) ?? id : 'General'),
  }
}
