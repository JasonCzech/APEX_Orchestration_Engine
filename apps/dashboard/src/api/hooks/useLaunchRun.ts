import { useMutation, useQueryClient } from '@tanstack/react-query'

import { queryKeys } from '@/api/queryKeys'
import { launchRun, type LaunchedRun, type LaunchRunInput } from '@/features/runs/launchRun'

/**
 * Launch mutation (D2 minimal launch): thread create + background run create
 * via the LangGraph SDK, then invalidate the pipelines façade list so the new
 * thread appears in the grid without waiting for the 15s poll. Navigation to
 * /runs/{threadId}?tab=activity is the caller's job (LaunchRunButton).
 */
export function useLaunchRun() {
  const queryClient = useQueryClient()
  return useMutation<LaunchedRun, Error, LaunchRunInput>({
    mutationFn: launchRun,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.pipelines.all })
    },
  })
}
