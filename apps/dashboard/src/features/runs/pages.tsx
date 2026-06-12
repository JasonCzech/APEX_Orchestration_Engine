import { FeaturePlaceholder } from '@/components/FeaturePlaceholder'

// D1: real read-path screens. RunDetailPage serves both /runs/:threadId
// (redirects to the current phase) and /runs/:threadId/phases/:phase.
export { RunsListPage } from './RunsListPage'
export { RunDetailPage, RunDetailPage as PhaseDetailPage } from './RunDetailPage'
export { TimelinePage as RunTimelinePage } from './TimelinePage'
export { ArtifactViewerPage } from '../artifacts/ArtifactViewerPage'

export function NewRunWizardPage() {
  return <FeaturePlaceholder title="New Run" route="/runs/new" />
}

export function RunsComparePage() {
  return <FeaturePlaceholder title="Compare Runs" route="/runs/compare" />
}
