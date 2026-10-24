import type { ReactNode } from 'react'
import { Link, useLocation, useMatches } from 'react-router'

import { isRouteHandle } from '@/routes/handle'
import { isDemoDataMode } from '@/config/runtimeConfig'
import { contextualHelp } from '@/features/help/contextualHelp'

import { useTopbarContributionSlot, useTopbarTitleOverride } from './TopbarContributionProvider'

/**
 * Sticky topbar: page title from the deepest route handle plus the
 * topbar-contribution slot for screen-published controls.
 */
export function Topbar({ navigation }: { navigation?: ReactNode } = {}) {
  const matches = useMatches()
  const location = useLocation()
  const help = contextualHelp(location.pathname, location.search)
  const override = useTopbarTitleOverride()
  const title =
    override ??
    [...matches].reverse().map((match) => match.handle).find(isRouteHandle)?.title ??
    'APEX Orchestration'
  const contribution = useTopbarContributionSlot()

  return (
    <header className="main-topbar">
      {navigation}
      <div className="main-topbar-page">
        <h1 className="topbar-page-title">{title}</h1>
        {isDemoDataMode() && <span className="topbar-meta-chip warning">DEMO · OFFLINE</span>}
      </div>
      {contribution !== null && (
        <div className="topbar-contribution-controls">{contribution}</div>
      )}
      {help && <div className="main-topbar-actions"><Link className="btn btn-secondary btn-sm" to={help}>Help for this page</Link></div>}
    </header>
  )
}
