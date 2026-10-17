import { Navigate, useLocation } from 'react-router'

/** Preserve saved preset URLs while using the user-facing name for new links. */
export function LegacyPresetRedirect() {
  const location = useLocation()
  return <Navigate replace to={{
    pathname: location.pathname.replace(/^\/golden-configs(?=\/|$)/, '/test-presets'),
    search: location.search,
    hash: location.hash,
  }} />
}
