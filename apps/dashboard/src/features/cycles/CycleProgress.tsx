import type { components } from '@apex/api-client'

const labels = { not_started: 'Not started', draft: 'In progress', in_review: 'In review', approved: 'Deliverables approved', stale: 'Approval stale', archived: 'Deliverables archived' }
const kinds = { scope: 'Scope', test_plan: 'Test plan', readiness: 'Readiness', script: 'Script', report: 'Report', postmortem: 'Postmortem' }
export function CycleProgress({ progress }: { progress?: components['schemas']['CycleProgress'] | null }) {
  if (!progress) return <p>Deliverable progress unavailable</p>
  return <section aria-label="Deliverable progress">
    <p><strong>{labels[progress.status]}</strong> · {progress.approved}/{progress.total} deliverables approved</p>
    <details><summary>Deliverable states</summary><ul>{progress.deliverables.map(item => <li key={item.kind}>{kinds[item.kind]}: {item.status === 'draft' ? 'Draft' : item.status === 'approved' ? 'Approved' : item.status === 'archived' ? 'Archived' : labels[item.status]}</li>)}</ul></details>
  </section>
}
