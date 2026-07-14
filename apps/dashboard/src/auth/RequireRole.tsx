import type { ReactNode } from 'react'

import type { Role } from '@/api/apexClient'

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
