import { FeaturePlaceholder } from '@/components/FeaturePlaceholder'

export function ConnectionsPage() {
  return <FeaturePlaceholder title="Connections" route="/admin/connections" />
}

export function ConnectionDetailPage() {
  return <FeaturePlaceholder title="Connection" route="/admin/connections/:id" />
}

export function ConsumersPage() {
  return <FeaturePlaceholder title="Consumers" route="/admin/consumers" />
}

export function ConsumerDetailPage() {
  return <FeaturePlaceholder title="Consumer" route="/admin/consumers/:id" />
}

export function AdminSystemPage() {
  return <FeaturePlaceholder title="System" route="/admin/system" />
}
