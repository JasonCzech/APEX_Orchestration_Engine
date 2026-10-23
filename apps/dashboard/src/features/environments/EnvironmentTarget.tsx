import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { environmentWriteMutationKey, environmentWriteMutationScopeId, type Environment } from '@/api/hooks/useEnvironments'
import { usePendingMutationCount } from '@/api/hooks/usePendingMutationCount'
import { queryKeys } from '@/api/queryKeys'
import { Dialog } from '@/components/Dialog'

/** Approves the current server-side target, never overwrites a newer URL. */
export function EnvironmentTarget({ environment, canApprove, onEdit }: {
  environment: Environment
  canApprove: boolean
  onEdit: () => void
}) {
  const [reviewVersion, setReviewVersion] = useState<number | null>(null)
  const targetChanged = reviewVersion !== null && reviewVersion !== environment.target_version
  const queryClient = useQueryClient()
  const writeCount = usePendingMutationCount(environmentWriteMutationKey(environment.id))
  const approve = useMutation({
    mutationKey: environmentWriteMutationKey(environment.id),
    scope: { id: environmentWriteMutationScopeId(environment.id) },
    mutationFn: async (version: number) => {
      const { data, error, response } = await getApexClient().POST('/v1/catalog/environments/{environment_id}/target-approval', {
        params: { path: { environment_id: environment.id } },
        body: { expected_target_version: version },
      })
      if (!response.ok || !data) throw new ApiError(response.status, errorMessageOf(error, 'Target approval could not be completed. Refresh its state before trying again.'), error)
      return data
    },
    onSuccess: data => {
      queryClient.setQueryData(queryKeys.catalog.environment(data.id), data)
      setReviewVersion(null)
    },
    onSettled: () => { void queryClient.invalidateQueries({ queryKey: queryKeys.catalog.environments() }) },
  })
  return <section className="env-card glass-panel" aria-labelledby="environment-target-heading">
    <h2 id="environment-target-heading" className="env-card-title">Execution target</h2>
    <p><span className={`status-badge ${environment.target_approved ? 'success' : environment.base_url ? 'warning' : 'neutral'}`}>{environment.target_approved ? 'Approved' : environment.base_url ? 'Needs approval' : 'No target'}</span></p>
    <p className="env-base-url">{environment.base_url ?? 'No target URL configured.'}</p>
    <p className="env-muted">External engines require an approved target. Simulation uses the environment as context only. Changing the target requires a new approval.</p>
    {!environment.target_approved && !canApprove && <p className="env-muted">Ask a global administrator to configure and approve this target.</p>}
    {canApprove && !environment.target_approved && (environment.base_url
      ? <button type="button" className="btn btn-primary" disabled={writeCount > 0} onClick={() => { approve.reset(); setReviewVersion(environment.target_version) }}>Review and approve target</button>
      : <button type="button" className="btn btn-secondary" disabled={writeCount > 0} onClick={onEdit}>Configure target URL</button>)}
    {reviewVersion !== null && <Dialog ariaLabel="Approve execution target" onClose={() => { if (!approve.isPending) setReviewVersion(null) }} overlayClassName="env-target-overlay" className="glass-panel env-target-dialog">
      <h2>Approve execution target?</h2>
      <p>This authorizes external test execution against <strong>{environment.base_url}</strong> for {environment.name}. Confirm that this destination is permitted for performance testing before approving.</p>
      <p>If the target changes before you approve, the approval is refused and you can review the new target.</p>
      {targetChanged && !approve.isError && <p className="tonal-card warning" role="alert">The target changed during this review. Close this review and check the updated target before approving.</p>}
      {!canApprove && <p className="tonal-card warning" role="alert">Target approval requires a global administrator. Your current permissions no longer allow this action.</p>}
      {approve.isError && <p className="tonal-card danger" role="alert">{approve.error.message} Close this review and check the refreshed target before trying again.</p>}
      <div className="env-panel-actions">
        <button type="button" className="btn btn-secondary" disabled={approve.isPending} onClick={() => setReviewVersion(null)}>Cancel</button>
        <button type="button" className="btn btn-primary" disabled={!canApprove || approve.isPending || approve.isError || targetChanged} onClick={() => approve.mutate(reviewVersion)}>{approve.isPending ? 'Approving…' : 'Approve target'}</button>
      </div>
    </Dialog>}
  </section>
}
