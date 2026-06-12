import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

interface TopbarContributionContextValue {
  contribution: ReactNode
  setContribution: (node: ReactNode) => void
}

const TopbarContributionContext = createContext<TopbarContributionContextValue | null>(null)

/**
 * APEX Load's topbar-contribution pattern: screens publish controls into the
 * sticky topbar instead of rendering their own header rows.
 */
export function TopbarContributionProvider({ children }: { children: ReactNode }) {
  const [contribution, setContribution] = useState<ReactNode>(null)
  const value = useMemo(() => ({ contribution, setContribution }), [contribution])
  return (
    <TopbarContributionContext.Provider value={value}>
      {children}
    </TopbarContributionContext.Provider>
  )
}

function useTopbarContributionContext(): TopbarContributionContextValue {
  const ctx = useContext(TopbarContributionContext)
  if (!ctx) {
    throw new Error('Topbar contributions require a TopbarContributionProvider')
  }
  return ctx
}

/** Read side — the Topbar renders whatever the active screen contributed. */
export function useTopbarContributionSlot(): ReactNode {
  return useTopbarContributionContext().contribution
}

/** Write side — screens contribute controls for the duration of their mount. */
export function useTopbarContribution(node: ReactNode): void {
  const { setContribution } = useTopbarContributionContext()
  useEffect(() => {
    setContribution(node)
    return () => setContribution(null)
  }, [node, setContribution])
}
