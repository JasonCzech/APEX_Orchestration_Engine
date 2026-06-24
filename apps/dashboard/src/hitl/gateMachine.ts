/**
 * HITL gate machine (plan Part 2 — "HITL gate machine", ratified design).
 *
 * Pure discriminated-union reducer — no React, no IO. Gate identity is the
 * `interrupt_id`: a GATE_DISCOVERED carrying a DIFFERENT id while a gate is on
 * screen means a NEW gate instance (discuss/revise re-interrupts mint fresh
 * ids), so the machine re-opens with a fresh draft; the SAME id is a no-op
 * (poll echoes must never clobber operator edits).
 *
 * Resume semantics are PESSIMISTIC: SUBMIT only parks the machine in
 * 'submitting' — the cache is never optimistically written. The mutation's
 * outcome events drive the next state:
 *   accepted + discuss/revise  -> awaiting_agent (the gate WILL reopen)
 *   accepted + approve/modify/skip_phase/abort -> no_gate (stream/poll
 *     narrative takes over)
 *   rejected(conflict)         -> superseded(by 'conflict')  [409 CAS loss]
 *   rejected(!conflict)        -> failed, DRAFT PRESERVED for [Retry]
 * An open gate that vanishes from the snapshot (GATE_CLEARED) was actioned
 * elsewhere -> superseded(by 'cleared').
 *
 * Every no-op returns the SAME state reference so effect loops that dispatch
 * idempotent discovery events settle without re-rendering.
 */
import type { GateInterruptPayload } from '@apex/pipeline-events'

export type GateKind = 'prompt_review' | 'phase_review'

/** Normalized pending interrupt (id + kind guaranteed; payload null on drift). */
export interface GateInstance {
  interrupt_id: string
  kind: GateKind
  phase: string
  /** Parsed payload; null when the zod contract rejected it (schema drift). */
  payload: GateInterruptPayload | null
}

export interface PromptDraft {
  system: string
  user: string
  application?: string
}

/** Operator working copy — only the keys the resume body can carry. */
export interface GateDraft {
  prompt?: PromptDraft
  instructions?: string
  message?: string
  note?: string
}

/** EDIT payload: partial prompt patches merge over the current draft. */
export interface GateDraftPatch {
  prompt?: Partial<PromptDraft>
  instructions?: string
  message?: string
  note?: string
}

export type GateAction = 'approve' | 'modify' | 'skip_phase' | 'abort' | 'revise' | 'discuss'

/** Actions whose 202 means "the agent is working and the gate will reopen". */
export type ReopeningAction = Extract<GateAction, 'discuss' | 'revise'>

export type GateMachineState =
  | { tag: 'no_gate' }
  | { tag: 'open'; gate: GateInstance; draft: GateDraft; dirty: boolean }
  | { tag: 'submitting'; gate: GateInstance; action: GateAction; draft: GateDraft }
  | { tag: 'awaiting_agent'; gate: GateInstance; action: ReopeningAction }
  | { tag: 'superseded'; gate: GateInstance; by: 'conflict' | 'cleared' }
  // `action` rides along (beyond the plan's minimal field list) so [Retry] can
  // resubmit the same action+draft without the view re-deriving it.
  | { tag: 'failed'; gate: GateInstance; action: GateAction; draft: GateDraft; error: Error }

export type GateEvent =
  | { type: 'GATE_DISCOVERED'; gate: GateInstance }
  | { type: 'GATE_CLEARED' }
  | { type: 'EDIT'; patch: GateDraftPatch }
  | { type: 'SUBMIT'; action: GateAction }
  | { type: 'RESUME_ACCEPTED' }
  | { type: 'RESUME_REJECTED'; error: Error; conflict: boolean }
  | { type: 'RESET' }

export const GATE_MACHINE_INITIAL: GateMachineState = { tag: 'no_gate' }

/** The gate currently bound to the machine (null only in no_gate). */
export function gateOf(state: GateMachineState): GateInstance | null {
  return state.tag === 'no_gate' ? null : state.gate
}

/** The draft carried by states that have one (open/submitting/failed). */
export function draftOf(state: GateMachineState): GateDraft | null {
  switch (state.tag) {
    case 'open':
    case 'submitting':
    case 'failed':
      return state.draft
    default:
      return null
  }
}

/** Original prompt text from the gate payload, normalized to strings. */
export function originalPromptOf(gate: GateInstance): PromptDraft {
  if (gate.payload?.kind === 'prompt_review') {
    const prompt: PromptDraft = {
      system: gate.payload.prompt.system ?? '',
      user: gate.payload.prompt.user ?? '',
    }
    if (gate.payload.prompt.application !== null && gate.payload.prompt.application !== undefined) {
      prompt.application = gate.payload.prompt.application
    }
    return prompt
  }
  return { system: '', user: '' }
}

/** Fresh draft for a newly discovered gate (prompt seeded from the payload). */
export function initialDraftFor(gate: GateInstance): GateDraft {
  if (gate.kind === 'prompt_review') return { prompt: originalPromptOf(gate) }
  return {}
}

/** Dirty = the draft prompt differs from the payload's original text. */
export function isPromptDirty(gate: GateInstance, draft: GateDraft): boolean {
  if (!draft.prompt) return false
  const original = originalPromptOf(gate)
  return (
    draft.prompt.system !== original.system ||
    draft.prompt.user !== original.user ||
    (draft.prompt.application ?? '') !== (original.application ?? '')
  )
}

function mergeDraft(gate: GateInstance, draft: GateDraft, patch: GateDraftPatch): GateDraft {
  const next: GateDraft = { ...draft }
  if (patch.prompt) {
    next.prompt = { ...originalPromptOf(gate), ...draft.prompt, ...patch.prompt }
  }
  if (patch.instructions !== undefined) next.instructions = patch.instructions
  if (patch.message !== undefined) next.message = patch.message
  if (patch.note !== undefined) next.note = patch.note
  return next
}

function openFor(gate: GateInstance): GateMachineState {
  return { tag: 'open', gate, draft: initialDraftFor(gate), dirty: false }
}

/**
 * Shared GATE_DISCOVERED handling for every state that already holds a gate:
 * same interrupt_id -> no-op (same reference), different id -> fresh 'open'.
 */
function rediscover(state: GateMachineState & { gate: GateInstance }, gate: GateInstance): GateMachineState {
  return gate.interrupt_id === state.gate.interrupt_id ? state : openFor(gate)
}

export function gateReducer(state: GateMachineState, event: GateEvent): GateMachineState {
  if (event.type === 'RESET') {
    // Full reset from anywhere; discovery (useGate) repopulates from the
    // refetched snapshot ("View current state" = refetch + RESET).
    return state.tag === 'no_gate' ? state : GATE_MACHINE_INITIAL
  }

  switch (state.tag) {
    case 'no_gate':
      return event.type === 'GATE_DISCOVERED' ? openFor(event.gate) : state

    case 'open':
      switch (event.type) {
        case 'GATE_DISCOVERED':
          return rediscover(state, event.gate)
        case 'GATE_CLEARED':
          // The pending interrupt vanished without us resuming it: another
          // operator (or surface) actioned the gate.
          return { tag: 'superseded', gate: state.gate, by: 'cleared' }
        case 'EDIT': {
          const draft = mergeDraft(state.gate, state.draft, event.patch)
          return { ...state, draft, dirty: isPromptDirty(state.gate, draft) }
        }
        case 'SUBMIT':
          return { tag: 'submitting', gate: state.gate, action: event.action, draft: state.draft }
        default:
          // RESUME_* without an in-flight submit is stale noise.
          return state
      }

    case 'submitting':
      switch (event.type) {
        case 'RESUME_ACCEPTED':
          return state.action === 'discuss' || state.action === 'revise'
            ? { tag: 'awaiting_agent', gate: state.gate, action: state.action }
            : GATE_MACHINE_INITIAL
        case 'RESUME_REJECTED':
          return event.conflict
            ? { tag: 'superseded', gate: state.gate, by: 'conflict' }
            : {
                tag: 'failed',
                gate: state.gate,
                action: state.action,
                draft: state.draft, // DRAFT PRESERVED for [Retry]
                error: event.error,
              }
        default:
          // Discovery/clear races mid-flight resolve via the mutation outcome
          // (a stale gate 409s into 'superseded'); edits/double-submits are
          // locked out while the resume is on the wire.
          return state
      }

    case 'awaiting_agent':
      switch (event.type) {
        case 'GATE_DISCOVERED':
          // New interrupt_id = the discuss/revise loop re-interrupted: a NEW
          // gate instance with a fresh draft. Same id = stale snapshot echo.
          return rediscover(state, event.gate)
        default:
          // GATE_CLEARED is expected here (the resume consumed the interrupt).
          return state
      }

    case 'superseded':
      switch (event.type) {
        case 'GATE_DISCOVERED':
          // A different gate replacing the superseded one opens directly; the
          // SAME id re-echoed by a stale cache must not clear the banner —
          // [View current state] (refetch + RESET) is the recovery path.
          return rediscover(state, event.gate)
        default:
          return state
      }

    case 'failed':
      switch (event.type) {
        case 'SUBMIT':
          // [Retry] resubmits (same action per the saved state, but any
          // explicit action is honored) with the preserved draft.
          return { tag: 'submitting', gate: state.gate, action: event.action, draft: state.draft }
        case 'EDIT': {
          const draft = mergeDraft(state.gate, state.draft, event.patch)
          return { ...state, draft }
        }
        case 'GATE_DISCOVERED':
          return rediscover(state, event.gate)
        case 'GATE_CLEARED':
          return { tag: 'superseded', gate: state.gate, by: 'cleared' }
        default:
          return state
      }
  }
}

/**
 * Resume body for POST /v1/pipelines/{thread_id}/gates/{interrupt_id}/resume —
 * exactly the keys the gate nodes consume per action (interrupts.ts decision
 * unions): modify -> prompt{system,user}, revise -> instructions,
 * discuss -> message; `note` rides along for attribution when present.
 */
export interface ResumeBody {
  action: GateAction
  prompt?: PromptDraft
  instructions?: string
  message?: string
  note?: string
}

export function buildResumeBody(action: GateAction, draft: GateDraft): ResumeBody {
  const body: ResumeBody = { action }
  if (action === 'modify' && draft.prompt) body.prompt = draft.prompt
  if (action === 'revise' && draft.instructions !== undefined) {
    body.instructions = draft.instructions
  }
  if (action === 'discuss' && draft.message !== undefined) body.message = draft.message
  if (draft.note) body.note = draft.note
  return body
}
