import { usePipelines } from '@/api/hooks/usePipelines'

/** Bounded shared title lookup; historic attempts retain their exact ID fallback. */
export function useRunNames(project?: string) {
  const runs = usePipelines({ project, limit: 100 })
  return new Map((runs.data?.items ?? []).map(run => [run.thread_id, run.title]))
}
