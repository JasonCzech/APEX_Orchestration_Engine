import { FeaturePlaceholder } from '@/components/FeaturePlaceholder'

export function RunsListPage() {
  return <FeaturePlaceholder title="Runs" route="/runs" />
}

export function NewRunWizardPage() {
  return <FeaturePlaceholder title="New Run" route="/runs/new" />
}

export function RunsComparePage() {
  return <FeaturePlaceholder title="Compare Runs" route="/runs/compare" />
}

export function RunDetailPage() {
  return <FeaturePlaceholder title="Run Detail" route="/runs/:threadId" />
}

export function PhaseDetailPage() {
  return <FeaturePlaceholder title="Phase Detail" route="/runs/:threadId/phases/:phase" />
}

export function RunTimelinePage() {
  return <FeaturePlaceholder title="Run Timeline" route="/runs/:threadId/timeline" />
}

export function ArtifactViewerPage() {
  return <FeaturePlaceholder title="Artifact Viewer" route="/runs/:threadId/artifacts/:name" />
}
