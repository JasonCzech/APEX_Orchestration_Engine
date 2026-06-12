import { FeaturePlaceholder } from '@/components/FeaturePlaceholder'

export function WorkItemsPage() {
  return <FeaturePlaceholder title="Work Items" route="/work-items" />
}

export function SavedQueriesPage() {
  return <FeaturePlaceholder title="Saved Queries" route="/work-items/saved" />
}

export function WorkItemDetailPage() {
  return <FeaturePlaceholder title="Work Item" route="/work-items/:provider/:itemId" />
}
