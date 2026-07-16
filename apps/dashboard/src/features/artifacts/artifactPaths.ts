/** Build the viewer route from opaque resource ids without changing path shape. */
export function artifactViewerPath(threadId: string, artifactId: string): string {
  return `/runs/${encodeURIComponent(threadId)}/artifacts/${encodeURIComponent(artifactId)}`
}
