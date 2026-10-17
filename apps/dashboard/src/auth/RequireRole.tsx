import type { ReactNode } from 'react'

import type { ConsumerInfo, Role } from '@/api/apexClient'

import { useOptionalConsumer } from './AuthProvider'

/** Ordered roles per the backend contract: viewer < operator < admin. */
const ROLE_ORDER: Record<Role, number> = {
  viewer: 0,
  operator: 1,
  admin: 2,
}

export function roleAtLeast(actual: Role, required: Role): boolean {
  return ROLE_ORDER[actual] >= ROLE_ORDER[required]
}

export function isGlobalAdmin(consumer: ConsumerInfo | null | undefined): boolean {
  return Boolean(consumer && consumer.role === 'admin' && consumer.scopes.length === 0)
}

export function hasFullProjectScope(
  consumer: ConsumerInfo | null | undefined,
  projectId: string,
): boolean {
  return Boolean(
    consumer &&
      (isGlobalAdmin(consumer) ||
        consumer.scopes.some((scope) => scope.project_id === projectId && !scope.app_id)),
  )
}

export function canMutateAudience(
  consumer: ConsumerInfo | null | undefined,
  projectId: string | null | undefined,
  appId: string | null | undefined,
): boolean {
  if (!consumer || !roleAtLeast(consumer.role, 'operator')) return false
  if (!projectId) return isGlobalAdmin(consumer)
  if (!appId) return hasFullProjectScope(consumer, projectId)
  return (
    isGlobalAdmin(consumer) ||
    consumer.scopes.some(
      (scope) =>
        scope.project_id === projectId && (!scope.app_id || scope.app_id === appId),
    )
  )
}

/**
 * Hides children from consumers below the required role. The server remains
 * the enforcement layer — this only shapes the UI (nav sections, admin routes).
 */
export function RequireRole({
  role,
  children,
  fallback = null,
}: {
  role: Role
  children: ReactNode
  fallback?: ReactNode
}) {
  const consumer = useOptionalConsumer()
  // Standalone component tests and embedded consumers may not mount AuthProvider.
  // Production routes always do, so an absent provider should not make controls
  // disappear in those isolated contexts.
  if (consumer === undefined) return <>{children}</>
  if (!consumer || !roleAtLeast(consumer.role, role)) return <>{fallback}</>
  return <>{children}</>
}

/** Renders controls for platform-wide administration, such as preset configuration. */
export function RequireGlobalAdmin({
  children,
  fallback = null,
}: {
  children: ReactNode
  fallback?: ReactNode
}) {
  const consumer = useOptionalConsumer()
  if (consumer === undefined) return <>{children}</>
  if (!isGlobalAdmin(consumer)) {
    return <>{fallback}</>
  }
  return <>{children}</>
}

/** Capability to create at least one pipeline, before choosing its audience. */
export function canLaunchPipelines(consumer: ConsumerInfo | null | undefined): boolean {
  return Boolean(consumer && roleAtLeast(consumer.role, 'operator') &&
    (isGlobalAdmin(consumer) || consumer.scopes.length > 0))
}

export function pipelineLaunchRefusal(consumer: ConsumerInfo | null | undefined): string | null {
  if (!consumer || !roleAtLeast(consumer.role, 'operator')) {
    return "Launching tests requires role 'operator' or higher."
  }
  if (!canLaunchPipelines(consumer)) {
    return 'Pipeline scope is not authorized. Ask an administrator to grant access to the project you need.'
  }
  return null
}
