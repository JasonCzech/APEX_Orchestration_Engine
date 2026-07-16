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
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from 'react'

import { useQueryClient } from '@tanstack/react-query'

import { parseGateInterrupt } from '@apex/pipeline-events'

import { useThreadState, type GateInterrupt } from '@/api/hooks/useThreadState'
import { queryKeys } from '@/api/queryKeys'

import {
  buildResumeBody,
  gateOf,
  gateReducer,
  GATE_MACHINE_INITIAL,
  isActionAllowedForGate,
  type GateAction,
  type GateDraftPatch,
  type GateInstance,
  type GateKind,
  type GateMachineState,
} from './gateMachine'
import { useResumeGate } from './useResumeGate'

function isGateKind(value: unknown): value is GateKind {
  return (
    value === 'prompt_review' ||
    value === 'phase_review' ||
    value === 'engine_provision_retry' ||
    value === 'engine_cleanup_retry' ||
    value === 'engine_collection_retry' ||
    value === 'engine_collection_settle_retry'
  )
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
      issueCount: drift.error.issues.length,
    })
  })
  const envelopeMatchesPayload =
    payload !== null &&
    payload.kind === interrupt.kind &&
    (interrupt.phase === null || interrupt.phase === undefined || interrupt.phase === payload.phase)
  if (payload !== null && !envelopeMatchesPayload) {
    // Never let a valid payload for one checkpoint authorize a different
    // facade interrupt. Keep the gate visible, but fail closed with no actions.
    console.warn('[useGate] gate payload did not match its interrupt envelope')
  }
  return {
    interrupt_id: interrupt.interrupt_id,
    kind: interrupt.kind,
    phase: interrupt.phase ?? payload?.phase ?? 'unknown',
    payload: envelopeMatchesPayload ? payload : null,
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
  return action === 'modify' || action === 'discuss' || action === 'revise' || action === 'retry'
}

function gatePayloadSignature(gate: GateInstance | null): string {
  return JSON.stringify(gate?.payload ?? null)
}

interface ReopeningBaseline {
  interruptId: string
  baselineUpdatedAt: number
  baselineServerUpdatedAt: string | null
  baselinePayload: string
  observedClear: boolean
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
  const settledGatePayloadRef = useRef<string | null>(null)
  const reopeningRef = useRef<ReopeningBaseline | null>(null)
  // Capture before the request starts. A poll can observe a same-id re-gate
  // while the 202 response is still in flight; taking the baseline in
  // onAccepted would mistake that new snapshot for the old generation.
  const submittedReopeningBaselineRef = useRef<ReopeningBaseline | null>(null)
  const [lastAccepted, setLastAccepted] = useState<GateResolution | null>(null)

  const resume = useResumeGate({
    onAccepted: (runId, variables) => {
      if (reopensGate(variables.body.action)) {
        // LangGraph can reuse the same interrupt id when the same node calls
        // interrupt() again. Wait for a post-resume snapshot generation, then
        // explicitly reopen even when the id is unchanged.
        const submitted = submittedReopeningBaselineRef.current
        reopeningRef.current =
          submitted?.interruptId === variables.interruptId
            ? submitted
            : {
                interruptId: variables.interruptId,
                baselineUpdatedAt: thread.dataUpdatedAt,
                baselineServerUpdatedAt: thread.data?.detail.updated_at ?? null,
                baselinePayload: gatePayloadSignature(gateOf(stateRef.current)),
                observedClear: false,
              }
      } else {
        settledGateIdRef.current = variables.interruptId
        settledGatePayloadRef.current = gatePayloadSignature(gateOf(stateRef.current))
      }
      submittedReopeningBaselineRef.current = null
      setLastAccepted({ interruptId: variables.interruptId, action: variables.body.action, runId })
      dispatch({ type: 'RESUME_ACCEPTED' })
    },
    onRejected: (rejection) => {
      submittedReopeningBaselineRef.current = null
      dispatch({ type: 'RESUME_REJECTED', ...rejection })
    },
  })
  const { mutate } = resume

  // Thread identity change = a different machine (cleanup runs before the new
  // thread's effects).
  useEffect(() => {
    return () => {
      settledGateIdRef.current = null
      settledGatePayloadRef.current = null
      reopeningRef.current = null
      submittedReopeningBaselineRef.current = null
      setLastAccepted(null)
      dispatch({ type: 'RESET' })
    }
  }, [threadId])

  const interrupts = thread.data?.interrupts
  const snapshotGate = useMemo(() => (interrupts ? firstGateOf(interrupts) : null), [interrupts])

  // Discovery must settle before paint. The run-detail workspace subscribes to
  // the same query independently and can otherwise commit its loaded state one
  // render before this reducer observes the interrupt, briefly showing the
  // ordinary review UI in place of the authoritative gate. Idempotent
  // dispatches keep the layout effect safe; state.tag in deps lets a RESET (or
  // an accepted resume that landed on no_gate) re-evaluate the cached snapshot.
  useLayoutEffect(() => {
    if (!interrupts) return
    if (snapshotGate) {
      const currentGate = gateOf(stateRef.current)
      const sameIdPayloadChanged =
        currentGate?.interrupt_id === snapshotGate.interrupt_id &&
        gatePayloadSignature(currentGate) !== gatePayloadSignature(snapshotGate)
      const reopening = reopeningRef.current
      if (reopening && snapshotGate.interrupt_id === reopening.interruptId) {
        const refreshed = thread.dataUpdatedAt > reopening.baselineUpdatedAt
        const payloadChanged = gatePayloadSignature(snapshotGate) !== reopening.baselinePayload
        const serverAdvanced =
          Boolean(thread.data?.detail.updated_at) &&
          thread.data?.detail.updated_at !== reopening.baselineServerUpdatedAt
        if (refreshed && (reopening.observedClear || payloadChanged || serverAdvanced)) {
          reopeningRef.current = null
          dispatch({ type: 'GATE_DISCOVERED', gate: snapshotGate, reopenSameId: true })
        } else {
          // Pre-resume cache echo: leave awaiting_agent intact.
          dispatch({ type: 'GATE_DISCOVERED', gate: snapshotGate })
        }
        return
      }
      if (reopening) reopeningRef.current = null
      const settledPayloadChanged =
        snapshotGate.interrupt_id === settledGateIdRef.current &&
        gatePayloadSignature(snapshotGate) !== settledGatePayloadRef.current
      if (snapshotGate.interrupt_id !== settledGateIdRef.current || settledPayloadChanged) {
        if (settledPayloadChanged) {
          settledGateIdRef.current = null
          settledGatePayloadRef.current = null
        }
        // LangGraph may reuse an interrupt id for a newer payload. Treat a
        // changed payload as a new gate even when no explicit re-gate action
        // originated in this tab; otherwise a second tab can approve stale UI.
        dispatch({ type: 'GATE_DISCOVERED', gate: snapshotGate, reopenSameId: sameIdPayloadChanged })
        return
      }
    }
    // No usable gate, or only the echo of one the server already resolved.
    let reopeningSettled = false
    if (!snapshotGate) {
      settledGateIdRef.current = null
      settledGatePayloadRef.current = null
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
    thread.data?.detail.updated_at,
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
      if (!isActionAllowedForGate(current.gate.kind, action)) return
      if (!current.gate.payload?.actions.some((advertised) => advertised === action)) return
      const submitEvent = { type: 'SUBMIT', action } as const
      const submitting = gateReducer(current, submitEvent)
      if (submitting === current) return
      if (reopensGate(action)) {
        submittedReopeningBaselineRef.current = {
          interruptId: current.gate.interrupt_id,
          baselineUpdatedAt: thread.dataUpdatedAt,
          baselineServerUpdatedAt: thread.data?.detail.updated_at ?? null,
          baselinePayload: gatePayloadSignature(current.gate),
          observedClear: false,
        }
      } else {
        submittedReopeningBaselineRef.current = null
      }
      // Close the same-tick window before React commits the reducer update.
      // Without this mirror, two click/keyboard submissions can issue two CAS
      // requests for the same interrupt.
      stateRef.current = submitting
      dispatch(submitEvent)
      mutate({
        threadId,
        interruptId: current.gate.interrupt_id,
        body: buildResumeBody(action, current.draft),
      })
    },
    [thread.data?.detail.updated_at, thread.dataUpdatedAt, threadId, mutate],
  )

  const reset = useCallback(() => dispatch({ type: 'RESET' }), [])

  const { refetch } = thread
  const viewCurrent = useCallback(() => {
    const current = stateRef.current
    // A superseded gate is settled by definition — do not let the pre-refetch
    // cache echo re-open it during the round trip.
    if (current.tag === 'superseded') {
      settledGateIdRef.current = current.gate.interrupt_id
      settledGatePayloadRef.current = gatePayloadSignature(current.gate)
    }
    dispatch({ type: 'RESET' })
    void refetch()
  }, [refetch])

  return { state, gate: gateOf(state), lastAccepted, edit, submit, reset, viewCurrent }
}
