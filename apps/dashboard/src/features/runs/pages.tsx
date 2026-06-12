// D1: real read-path screens. RunDetailPage serves both /runs/:threadId
// (redirects to the current phase) and /runs/:threadId/phases/:phase.
export { RunsListPage } from './RunsListPage'
export { RunDetailPage, RunDetailPage as PhaseDetailPage } from './RunDetailPage'
export { TimelinePage as RunTimelinePage } from './TimelinePage'
export { ArtifactViewerPage } from '../artifacts/ArtifactViewerPage'

// D4: the 6-step new-run wizard (src/features/new-test).
export { NewRunWizardPage } from '../new-test/NewRunWizard'

// D8: side-by-side run comparison (src/features/compare) — last placeholder swapped.
export { ComparePage as RunsComparePage } from '../compare/ComparePage'
