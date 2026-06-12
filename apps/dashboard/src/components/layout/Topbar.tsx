import { useMatches } from 'react-router'

import { isRouteHandle } from '@/routes/handle'

import { useTopbarContributionSlot } from './TopbarContributionProvider'

/**
 * Sticky topbar: page title from the deepest route handle plus the
 * topbar-contribution slot for screen-published controls.
 */
export function Topbar() {
  const matches = useMatches()
  const title =
    [...matches].reverse().map((match) => match.handle).find(isRouteHandle)?.title ??
    'APEX Orchestration'
  const contribution = useTopbarContributionSlot()

  return (
    <header className="main-topbar">
      <div className="main-topbar-page">
        <h1 className="topbar-page-title">{title}</h1>
      </div>
      {contribution !== null && (
        <div className="topbar-contribution-controls">{contribution}</div>
      )}
      <div className="main-topbar-actions" />
    </header>
  )
}
