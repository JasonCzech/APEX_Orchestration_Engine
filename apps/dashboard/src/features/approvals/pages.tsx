import { FeaturePlaceholder } from '@/components/FeaturePlaceholder'

export function ApprovalsInboxPage() {
  return <FeaturePlaceholder title="Approvals" route="/approvals" />
}

export function ApprovalDetailPage() {
  return <FeaturePlaceholder title="Approval" route="/approvals/:threadId/:interruptId" />
}
