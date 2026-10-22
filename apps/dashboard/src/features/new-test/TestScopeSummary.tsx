import { useApplications, useEnvironments } from '@/api/hooks/useCatalog'
import { parseTestScope, type TestParticipant } from './testScope'
function Participant({ p, primary, traffic }: { p: TestParticipant; primary: boolean; traffic: boolean }) {
  const apps = useApplications(p.project_id)
  const envs = useEnvironments(p.application_id)
  return <li><strong>{p.layer === 'middle' ? 'Middle tier' : p.layer}</strong>: {apps.data?.find(a => a.id === p.application_id)?.name ?? p.application_id} · {envs.data?.find(e => e.id === p.environment_id)?.name ?? p.environment_id} · {p.project_id}{primary && ' · Primary system'}{traffic && ' · Traffic entry'}</li>
}
export function TestScopeSummary({ value }: { value: unknown }) {
  const scope = parseTestScope(value)
  if (!scope) return <p>Detailed topology was not recorded.</p>
  return <section aria-label="End-to-end test scope"><ul>{[...scope.participants].sort((a, b) => ['frontend', 'middle', 'backend'].indexOf(a.layer) - ['frontend', 'middle', 'backend'].indexOf(b.layer)).map(p => <Participant key={p.id} p={p} primary={p.id === scope.primary_participant_id} traffic={p.id === scope.traffic_entry_participant_id} />)}</ul></section>
}
