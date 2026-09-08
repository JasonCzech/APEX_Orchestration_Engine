import { createContext, useContext, useEffect, useId } from 'react'
import type { Blocker } from 'react-router'

const unblocked: Blocker = {
  state: 'unblocked',
  location: undefined,
  proceed: undefined,
  reset: undefined,
}
export interface Guard {
  id: string
  priority: number
}
export const NavigationGuardContext = createContext<{
  register: (guard: Guard) => () => void
  blocker: Blocker
  owner?: string
} | null>(null)

export function useNavigationBlocker(active: boolean, priority = 0): Blocker {
  const context = useContext(NavigationGuardContext)
  if (!context) throw new Error('Navigation guard requires the app shell')
  const id = useId()
  const { register } = context
  useEffect(() => {
    if (active) return register({ id, priority })
  }, [active, id, priority, register])
  return context.owner === id ? context.blocker : unblocked
}
