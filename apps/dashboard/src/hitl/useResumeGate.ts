/**
 * useResumeGate — react-query mutation for the CAS resume route
 * POST /v1/pipelines/{thread_id}/gates/{interrupt_id}/resume (typed via the
 * generated @apex/api-client `resumeGate` operation).
 *
 * Outcome mapping (plan "HITL gate machine"):
 *   202 {run_id}        -> onAccepted + invalidate threads.state(threadId)
 *                          and every pipelines list (inbox rows poll there)
 *   409 problem+json
 *     title gate_superseded -> onRejected {conflict: true}  (stale interrupt
 *                          OR concurrent resume — the multitask CAS reject)
 *   anything else (422/5xx/network) -> onRejected {conflict: false}
 *
 * PESSIMISTIC by design: no cache writes before the 202 lands.
 */
import { useMutation, useQueryClient, type UseMutationResult } from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf, isApiError } from '@/api/errors'
import { queryKeys } from '@/api/queryKeys'

import type { ResumeBody } from './gateMachine'

export type ResumeGateResponse = components['schemas']['ResumeGateResponse']

export interface ResumeGateVariables {
  threadId: string
  interruptId: string
  body: ResumeBody
}

export interface ResumeRejection {
  error: Error
  /** True when the CAS lost: the interrupt is stale or someone else resumed. */
  conflict: boolean
}

/** RFC-7807 title extraction (`problem()` bodies: {type, title, status, ...}). */
function problemTitleOf(body: unknown): string | null {
  if (body !== null && typeof body === 'object' && 'title' in body) {
    const title = (body as { title?: unknown }).title
    if (typeof title === 'string') return title
  }
  return null
}

/** 409 + title "gate_superseded" — the backend's CAS reject on this route. */
export function isGateSupersededError(error: unknown): boolean {
  return (
    isApiError(error) && error.status === 409 && problemTitleOf(error.body) === 'gate_superseded'
  )
}

export function classifyResumeError(error: unknown): ResumeRejection {
  return {
    error: error instanceof Error ? error : new Error(String(error)),
    conflict: isGateSupersededError(error),
  }
}

async function postResume({
  threadId,
  interruptId,
  body,
}: ResumeGateVariables): Promise<ResumeGateResponse> {
  const { data, error, response } = await getApexClient().POST(
    '/v1/pipelines/{thread_id}/gates/{interrupt_id}/resume',
    {
      params: { path: { thread_id: threadId, interrupt_id: interruptId } },
      // ResumeGateRequest types `prompt` as an open record; PromptDraft is the
      // closed {system, user} shape — widen at the wire boundary only.
      body: { ...body, prompt: body.prompt as Record<string, unknown> | undefined },
    },
  )
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Gate resume failed (${response.status})`),
      error,
    )
  }
  return data
}

export interface UseResumeGateOptions {
  onAccepted?: (runId: string, variables: ResumeGateVariables) => void
  onRejected?: (rejection: ResumeRejection, variables: ResumeGateVariables) => void
}

export function useResumeGate(
  options: UseResumeGateOptions = {},
): UseMutationResult<ResumeGateResponse, Error, ResumeGateVariables> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: postResume,
    onSuccess: (data, variables) => {
      // 202 accepted: heal the snapshot and the fleet lists; the refetched
      // interrupts drive the machine (GATE_CLEARED / new GATE_DISCOVERED).
      void queryClient.invalidateQueries({
        queryKey: queryKeys.threads.state(variables.threadId),
      })
      void queryClient.invalidateQueries({ queryKey: queryKeys.pipelines.lists() })
      void queryClient.invalidateQueries({ queryKey: queryKeys.approvals.inbox() })
      options.onAccepted?.(data.run_id, variables)
    },
    onError: (error, variables) => {
      options.onRejected?.(classifyResumeError(error), variables)
    },
  })
}
