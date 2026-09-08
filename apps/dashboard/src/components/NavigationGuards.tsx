import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { useBlocker, type Blocker } from 'react-router'
import {
  NavigationGuardContext as Context,
  type Guard,
} from './useNavigationBlocker'

/** React Router supports one blocker. Serialize independent safeguards before navigating. */
export function NavigationGuards({ children }: { children: ReactNode }) {
  const [guards, setGuards] = useState<Guard[]>([])
  const [waived, setWaived] = useState<string[]>([])
  const blocker = useBlocker(guards.length > 0)
  const owner = [...guards]
    .sort((a, b) => b.priority - a.priority)
    .find((guard) => !waived.includes(guard.id))?.id
  const register = useCallback((guard: Guard) => {
    setGuards((previous) => [
      ...previous.filter((item) => item.id !== guard.id),
      guard,
    ])
    return () =>
      setGuards((previous) => previous.filter((item) => item.id !== guard.id))
  }, [])
  useEffect(() => {
    if (blocker.state === 'unblocked')
      setWaived((previous) => (previous.length ? [] : previous))
  }, [blocker.state])
  useEffect(() => {
    if (blocker.state === 'blocked' && !owner) blocker.proceed()
  }, [blocker, owner])
  const shared = useMemo<Blocker>(
    () =>
      blocker.state === 'blocked'
        ? {
            ...blocker,
            proceed: () => {
              if (owner) setWaived((previous) => [...previous, owner])
              else blocker.proceed()
            },
            reset: () => {
              setWaived([])
              blocker.reset()
            },
          }
        : blocker,
    [blocker, owner],
  )
  return (
    <Context.Provider value={{ register, blocker: shared, owner }}>
      {children}
    </Context.Provider>
  )
}
