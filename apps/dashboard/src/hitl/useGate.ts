/**
 * useGate(threadId) — binds the pure gate machine to the data layer.
 *
 * Sources:
 * - useThreadState's `interrupts` (the SAME cache entry the run-detail page
 *   polls): first usable interrupt -> GATE_DISCOVERED, none -> GATE_CLEARED.
 *   The reducer's identity rules make these dispatches idempotent, so the
 *   discovery effect re-runs freely (including on machine-tag changes, which
 *   lets a RESET rediscover the still-cached gate).
 * - The stream's pendingGateHint as an ACCELERATOR only: a hint object with no
 *   hydrated interrupt triggers one snapshot refetch per hint identity (the
 *   gate_opened event fires seconds before the 10s poll would land). Identity
 *   is the OBJECT reference — modify/discuss/revise re-gates carry the same
 *   gate/phase values but the stream reducer mints a fresh hint object per
 *   gate_opened event.
 *
 * Settled-gate suppression: after a terminal 202 (and after "View current
 * state" on a superseded gate) the consumed interrupt_id is remembered so a
 * stale cache echo cannot re-open a gate the server already resolved. Reopening
 * actions instead wait for a refreshed snapshot generation.
 *
 * Resume wiring is PESSIMISTIC: submit() -> SUBMIT (machine shows in-flight)
 * -> POST; 202 -> RESUME_ACCEPTED (+ invalidations inside useResumeGate);
 * 409 gate_superseded -> RESUME_REJECTED{conflict} -> 'superseded'; other
 * failures -> 'failed' with the draft preserved. Reopening actions re-interrupt
 * via the stream/poll; a refreshed snapshot opens the next review even when
 * LangGraph derives the same interrupt_id for the repeated node.
 */
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'

import { useQueryClient } from '@tanstack/react-query'

import { parseGateInterrupt } from '@apex/pipeline-events'

import { useThreadState, type GateInterrupt } from '@/api/hooks/useThreadState'
import { queryKeys } from '@/api/queryKeys'

import {
  buildResumeBody,
  gateOf,
  gateReducer,
  GATE_MACHINE_INITIAL,
  type GateAction,
  type GateDraftPatch,
  type GateInstance,
  type GateKind,
  type GateMachineState,
} from './gateMachine'
import { useResumeGate } from './useResumeGate'

function isGateKind(value: unknown): value is GateKind {
  return value === 'prompt_review' || value === 'phase_review'
}

/**
 * Normalize a facade GateInterrupt row to a GateInstance. Unusable rows
 * (missing id / unknown kind) return null — an unknown kind must not unrender
 * an existing gate; it is logged as drift and skipped.
 */
export function normalizeGateInterrupt(interrupt: GateInterrupt): GateInstance | null {
  if (!interrupt.interrupt_id || !isGateKind(interrupt.kind)) return null
  const payload = parseGateInterrupt(interrupt.payload, (drift) => {
    console.warn('[useGate] gate payload rejected by the zod contract', {
      interruptId: interrupt.interrupt_id,
      issues: drift.error.issues,
    })
  })
  return {
    interrupt_id: interrupt.interrupt_id,
    kind: interrupt.kind,
    phase: interrupt.phase ?? payload?.phase ?? 'unknown',
    payload,
  }
}

/** First usable pending gate of the snapshot (the facade lists one today). */
export function firstGateOf(interrupts: GateInterrupt[]): GateInstance | null {
  for (const interrupt of interrupts) {
    const gate = normalizeGateInterrupt(interrupt)
    if (gate) return gate
  }
  return null
}

/** Loose hint shape (mirrors liveTypes.LiveGateHint without importing it). */
export type GateHintLike = { gate?: string | null; phase?: string | null } | string | null

export interface UseGateOptions {
  /** Stream accelerator (useRunLiveness().stream.pendingGateHint). */
  gateHint?: GateHintLike
}

/** Record of the latest 202-accepted resume (terminal for that interrupt). */
export interface GateResolution {
  interruptId: string
  action: GateAction
  runId: string
}

export interface UseGateResult {
  state: GateMachineState
  /** Gate bound to the machine (null only in no_gate). */
  gate: GateInstance | null
  /**
   * Latest accepted resume, or null. Consumers needing a terminal signal
   * (inbox onOutcome) read THIS rather than sniffing transient machine
   * states — the 'submitting' commit can be batched away entirely when the
   * 202 lands fast.
   */
  lastAccepted: GateResolution | null
  edit: (patch: GateDraftPatch) => void
  submit: (action: GateAction) => void
  reset: () => void
  /** "View current state": refetch the snapshot AND reset the machine. */
  viewCurrent: () => void
}

function reopensGate(action: GateAction): boolean {
  return action === 'modify' || action === 'discuss' || action === 'revise'
}

function gatePayloadSignature(gate: GateInstance | null): string {
  return JSON.stringify(gate?.payload ?? null)
}

export function useGate(threadId: string, options: UseGateOptions = {}): UseGateResult {
  const queryClient = useQueryClient()
  const thread = useThreadState(threadId)
  const [state, dispatch] = useReducer(gateReducer, GATE_MACHINE_INITIAL)

  // Render-time mirror so callbacks read the committed state without
  // re-memoizing per keystroke.
  const stateRef = useRef(state)
  stateRef.current = state

  // interrupt_id the server already resolved (202 / superseded + viewCurrent):
  // suppresses stale cache echoes until the refetched snapshot moves on.
  const settledGateIdRef = useRef<string | null>(null)
  const reopeningRef = useRef<{
    interruptId: string
    baselineUpdatedAt: number
    baselinePayload: string
    observedClear: boolean
  } | null>(null)
  const [lastAccepted, setLastAccepted] = useState<GateResolution | null>(null)

  const resume = useResumeGate({
    onAccepted: (runId, variables) => {
      if (reopensGate(variables.body.action)) {
        // LangGraph can reuse the same interrupt id when the same node calls
        // interrupt() again. Wait for a post-resume snapshot generation, then
        // explicitly reopen even when the id is unchanged.
        reopeningRef.current = {
          interruptId: variables.interruptId,
          baselineUpdatedAt: thread.dataUpdatedAt,
          baselinePayload: gatePayloadSignature(gateOf(stateRef.current)),
          observedClear: false,
        }
      } else {
        settledGateIdRef.current = variables.interruptId
      }
      setLastAccepted({ interruptId: variables.interruptId, action: variables.body.action, runId })
      dispatch({ type: 'RESUME_ACCEPTED' })
    },
    onRejected: (rejection) => dispatch({ type: 'RESUME_REJECTED', ...rejection }),
  })
  const { mutate } = resume

  // Thread identity change = a different machine (cleanup runs before the new
  // thread's effects).
  useEffect(() => {
    return () => {
      settledGateIdRef.current = null
      reopeningRef.current = null
      setLastAccepted(null)
      dispatch({ type: 'RESET' })
    }
  }, [threadId])

  const interrupts = thread.data?.interrupts
  const snapshotGate = useMemo(() => (interrupts ? firstGateOf(interrupts) : null), [interrupts])

  // Discovery: idempotent dispatches; state.tag in deps lets a RESET (or an
  // accepted resume that landed on no_gate) re-evaluate the cached snapshot.
  useEffect(() => {
    if (!interrupts) return
    if (snapshotGate) {
      const reopening = reopeningRef.current
      if (reopening && snapshotGate.interrupt_id === reopening.interruptId) {
        const refreshed = thread.dataUpdatedAt > reopening.baselineUpdatedAt
        const payloadChanged = gatePayloadSignature(snapshotGate) !== reopening.baselinePayload
        if (refreshed && (reopening.observedClear || payloadChanged)) {
          reopeningRef.current = null
          dispatch({ type: 'GATE_DISCOVERED', gate: snapshotGate, reopenSameId: true })
        } else {
          // Pre-resume cache echo: leave awaiting_agent intact.
          dispatch({ type: 'GATE_DISCOVERED', gate: snapshotGate })
        }
        return
      }
      if (reopening) reopeningRef.current = null
      if (snapshotGate.interrupt_id !== settledGateIdRef.current) {
        dispatch({ type: 'GATE_DISCOVERED', gate: snapshotGate })
        return
      }
    }
    // No usable gate, or only the echo of one the server already resolved.
    let reopeningSettled = false
    if (!snapshotGate) {
      settledGateIdRef.current = null
      const reopening = reopeningRef.current
      if (reopening) {
        reopening.observedClear = true
        const refreshed = thread.dataUpdatedAt > reopening.baselineUpdatedAt
        const threadStatus = thread.data?.detail.thread_status
        reopeningSettled =
          refreshed && (threadStatus === 'idle' || threadStatus === 'error')
        if (reopeningSettled) reopeningRef.current = null
      }
    }
    dispatch({ type: 'GATE_CLEARED', settled: reopeningSettled })
  }, [
    interrupts,
    snapshotGate,
    state.tag,
    thread.data?.detail.thread_status,
    thread.dataUpdatedAt,
  ])

  // Hint accelerator: handled once per hint object identity; only fires when
  // nothing hydrated is on screen (no_gate) or a re-gate is expected
  // (awaiting_agent after modify/discuss/revise).
  const hint = options.gateHint ?? null
  const handledHintRef = useRef<GateHintLike>(null)
  useEffect(() => {
    handledHintRef.current = null
  }, [threadId])
  useEffect(() => {
    if (!hint) {
      handledHintRef.current = null
      return
    }
    if (handledHintRef.current === hint) return
    if (state.tag === 'no_gate' || state.tag === 'awaiting_agent') {
      handledHintRef.current = hint
      void queryClient.invalidateQueries({ queryKey: queryKeys.threads.state(threadId) })
    }
  }, [hint, state.tag, threadId, queryClient])

  const edit = useCallback((patch: GateDraftPatch) => dispatch({ type: 'EDIT', patch }), [])

  const submit = useCallback(
    (action: GateAction) => {
      const current = stateRef.current
      // Only an open or failed (retry) gate can submit; everything else is
      // either already in flight or has nothing to resume.
      if (current.tag !== 'open' && current.tag !== 'failed') return
      dispatch({ type: 'SUBMIT', action })
      mutate({
        threadId,
        interruptId: current.gate.interrupt_id,
        body: buildResumeBody(action, current.draft),
      })
    },
    [threadId, mutate],
  )

  const reset = useCallback(() => dispatch({ type: 'RESET' }), [])

  const { refetch } = thread
  const viewCurrent = useCallback(() => {
    const current = stateRef.current
    // A superseded gate is settled by definition — do not let the pre-refetch
    // cache echo re-open it during the round trip.
    if (current.tag === 'superseded') settledGateIdRef.current = current.gate.interrupt_id
    dispatch({ type: 'RESET' })
    void refetch()
  }, [refetch])

  return { state, gate: gateOf(state), lastAccepted, edit, submit, reset, viewCurrent }
}
