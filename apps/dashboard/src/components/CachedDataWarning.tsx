export function CachedDataWarning({
  error,
  onRetry,
}: {
  error: unknown
  onRetry: () => void
}) {
  const message = error instanceof Error ? error.message : 'Unknown refresh error'
  return (
    <div className="tonal-card danger" role="alert">
      Showing cached data — the latest refresh failed: {message}{' '}
      <button type="button" className="btn btn-ghost btn-sm" onClick={onRetry}>
        Retry
      </button>
    </div>
  )
}
