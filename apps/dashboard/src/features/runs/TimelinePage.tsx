import { useParams, useSearchParams } from 'react-router'

import { RunWorkspace } from './RunDetailPage'

/** /runs/:threadId/timeline — chronological audit ledger from the snapshot. */
export function TimelinePage() {
  const { threadId = '' } = useParams()
  const [searchParams] = useSearchParams()
  return (
    <RunWorkspace
      key={threadId}
      threadId={threadId}
      section="timeline"
      phase={searchParams.get('phase')}
    />
  )
}
