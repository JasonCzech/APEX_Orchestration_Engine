import { createContext, useContext, useEffect, useId } from 'react'
import type { Blocker, Location } from 'react-router'

const unblocked: Blocker = {
  state: 'unblocked',
  location: undefined,
  proceed: undefined,
  reset: undefined,
}
export type NavigationBlockPredicate = (args: {
  currentLocation: Location
  nextLocation: Location
}) => boolean

export interface Guard {
  id: string
  priority: number
  shouldBlock?: NavigationBlockPredicate
}
export const NavigationGuardContext = createContext<{
  register: (guard: Guard) => () => void
  blocker: Blocker
  owner?: string
} | null>(null)

export function useNavigationBlocker(
  active: boolean,
  priority = 0,
  shouldBlock?: NavigationBlockPredicate,
): Blocker {
  const context = useContext(NavigationGuardContext)
  const id = useId()
  const register = context?.register
  useEffect(() => {
    if (active && register) return register({ id, priority, shouldBlock })
  }, [active, id, priority, register, shouldBlock])
  if (!context) return unblocked
  return context.owner === id ? context.blocker : unblocked
}

