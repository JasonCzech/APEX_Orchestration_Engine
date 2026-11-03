import type { ComponentProps } from 'react'
import { Link, useLocation } from 'react-router'

/** Only return to an operator surface in this application. */
export function artifactReturnPath(value: unknown, threadId: string): string {
  if (typeof value !== 'string' || Array.from(value).some((character) => character.charCodeAt(0) <= 32 || character === '\\')) return `/runs/${threadId}`
  if (!/^\/(?:approvals(?:\/[^/?#]+\/[^/?#]+)?|runs\/[^/?#]+(?:\/(?:phases|reviews)\/[^/?#]+)?|evidence)(?:\?[^#]*)?$/.test(value)) return `/runs/${threadId}`
  return value
}

export function ArtifactLink({
  returnTo,
  artifactRef,
  ...props
}: ComponentProps<typeof Link> & { returnTo?: string; artifactRef?: string }) {
  const location = useLocation()
  return (
    <Link
      {...props}
      state={{
        artifactReturnTo: returnTo ?? location.pathname + location.search,
        ...(artifactRef ? { artifactRef } : {}),
      }}
    />
  )
}
