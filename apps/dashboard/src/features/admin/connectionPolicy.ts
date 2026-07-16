import type { PortKind } from '@/api/hooks/useConnections'

const KUBERNETES_IN_CLUSTER_AUTH_MODES = new Set(['in_cluster', 'in-cluster', 'incluster'])
const RUNTIME_IDENTITY_KINDS = new Set<PortKind>(['artifact_store', 'execution_engine'])

export function isRuntimeIdentityKind(kind: PortKind): boolean {
  return RUNTIME_IDENTITY_KINDS.has(kind)
}

/** Mirrors the server policies that make a connection global-admin-only. */
export function scopedConnectionPolicyIssue(
  kind: PortKind,
  provider: string,
  options: Record<string, unknown>,
): string | null {
  if (Object.keys(options).some((key) => key.startsWith('_apex_'))) {
    return 'Options beginning with _apex_ require a global administrator.'
  }
  if (kind !== 'cluster_inventory' || provider.trim().toLowerCase() !== 'kubernetes') {
    return null
  }
  const authMode = String(options['auth_mode'] ?? 'bearer').trim().toLowerCase()
  return KUBERNETES_IN_CLUSTER_AUTH_MODES.has(authMode)
    ? 'Kubernetes in-cluster authentication requires a global administrator.'
    : null
}
