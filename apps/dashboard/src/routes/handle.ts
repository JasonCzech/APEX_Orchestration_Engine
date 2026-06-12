/** Route handles carry the topbar title for each screen. */
export interface RouteHandle {
  title: string
}

export function isRouteHandle(handle: unknown): handle is RouteHandle {
  return (
    handle !== null &&
    typeof handle === 'object' &&
    'title' in handle &&
    typeof (handle as { title?: unknown }).title === 'string'
  )
}
