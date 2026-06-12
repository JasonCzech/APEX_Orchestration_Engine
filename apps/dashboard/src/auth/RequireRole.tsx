import type { ReactNode } from 'react'

import type { Role } from '@/api/apexClient'

import { useConsumer } from './AuthProvider'

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
  const consumer = useConsumer()
  if (!consumer || !roleAtLeast(consumer.role, role)) return <>{fallback}</>
  return <>{children}</>
}
