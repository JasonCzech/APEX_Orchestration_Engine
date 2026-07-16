/**
 * Exhaustive table-driven coverage of the gate machine: every state x every
 * event, plus the draft/dirty mechanics and the resume-body builder.
 */
import { describe, expect, it } from 'vitest'

import {
  buildResumeBody,
  gateReducer,
  GATE_MACHINE_INITIAL,
  initialDraftFor,
  isPromptDirty,
  type GateEvent,
  type GateInstance,
  type GateMachineState,
} from '@/hitl/gateMachine'

import {
  engineRetryInterrupt,
  gateInstanceOf,
  phaseInterrupt,
  promptInterrupt,
} from './gateFixtures'

const promptGate: GateInstance = gateInstanceOf(promptInterrupt('int-1'))
const otherGate: GateInstance = gateInstanceOf(promptInterrupt('int-2'))
const phaseGate: GateInstance = gateInstanceOf(phaseInterrupt('int-9'))
const retryGate: GateInstance = gateInstanceOf(engineRetryInterrupt('int-retry'))

const failure = new Error('resume exploded')

/** Canonical instance of every machine state (over the SAME gate, int-1). */
const STATES: Record<string, GateMachineState> = {
  no_gate: GATE_MACHINE_INITIAL,
  open: { tag: 'open', gate: promptGate, draft: initialDraftFor(promptGate), dirty: false },
  submitting_approve: {
    tag: 'submitting',
    gate: promptGate,
    action: 'approve',
    draft: initialDraftFor(promptGate),
  },
  submitting_discuss: { tag: 'submitting', gate: phaseGate, action: 'discuss', draft: { message: 'why?' } },
  awaiting_agent: { tag: 'awaiting_agent', gate: phaseGate, action: 'discuss' },
  superseded: { tag: 'superseded', gate: promptGate, by: 'conflict' },
  failed: {
    tag: 'failed',
    gate: promptGate,
    action: 'modify',
    draft: { prompt: { system: 'EDITED', user: 'kept' } },
    error: failure,
  },
}

/** Same-id rediscovery must reuse the SAME gate identity as each state above. */
function sameIdGateFor(state: GateMachineState): GateInstance {
  return state.tag === 'no_gate' ? promptGate : state.gate
}

const EVENTS: Record<string, (state: GateMachineState) => GateEvent> = {
  DISCOVERED_same: (state) => ({ type: 'GATE_DISCOVERED', gate: sameIdGateFor(state) }),
  DISCOVERED_diff: () => ({ type: 'GATE_DISCOVERED', gate: otherGate }),
  GATE_CLEARED: () => ({ type: 'GATE_CLEARED' }),
  EDIT: () => ({ type: 'EDIT', patch: { note: 'fyi' } }),
  SUBMIT: () => ({ type: 'SUBMIT', action: 'approve' }),
  RESUME_ACCEPTED: () => ({ type: 'RESUME_ACCEPTED' }),
  REJECTED_conflict: () => ({ type: 'RESUME_REJECTED', error: failure, conflict: true }),
  REJECTED_other: () => ({ type: 'RESUME_REJECTED', error: failure, conflict: false }),
  RESET: () => ({ type: 'RESET' }),
}

/** Expected resulting TAG for every state x event ('=' marks same-reference no-op). */
const TABLE: Record<string, Record<string, string>> = {
  no_gate: {
    DISCOVERED_same: 'open',
    DISCOVERED_diff: 'open',
    GATE_CLEARED: '=',
    EDIT: '=',
    SUBMIT: '=',
    RESUME_ACCEPTED: '=',
    REJECTED_conflict: '=',
    REJECTED_other: '=',
    RESET: '=',
  },
  open: {
    DISCOVERED_same: '=',
    DISCOVERED_diff: 'open',
    GATE_CLEARED: 'superseded',
    EDIT: 'open',
    SUBMIT: 'submitting',
    RESUME_ACCEPTED: '=',
    REJECTED_conflict: '=',
    REJECTED_other: '=',
    RESET: 'no_gate',
  },
  submitting_approve: {
    DISCOVERED_same: '=',
    DISCOVERED_diff: '=',
    GATE_CLEARED: '=',
    EDIT: '=',
    SUBMIT: '=',
    RESUME_ACCEPTED: 'no_gate',
    REJECTED_conflict: 'superseded',
    REJECTED_other: 'failed',
    RESET: 'no_gate',
  },
  submitting_discuss: {
    DISCOVERED_same: '=',
    DISCOVERED_diff: '=',
    GATE_CLEARED: '=',
    EDIT: '=',
    SUBMIT: '=',
    RESUME_ACCEPTED: 'awaiting_agent',
    REJECTED_conflict: 'superseded',
    REJECTED_other: 'failed',
    RESET: 'no_gate',
  },
  awaiting_agent: {
    DISCOVERED_same: '=',
    DISCOVERED_diff: 'open',
    GATE_CLEARED: '=',
    EDIT: '=',
    SUBMIT: '=',
    RESUME_ACCEPTED: '=',
    REJECTED_conflict: '=',
    REJECTED_other: '=',
    RESET: 'no_gate',
  },
  superseded: {
    DISCOVERED_same: '=',
    DISCOVERED_diff: 'open',
    GATE_CLEARED: '=',
    EDIT: '=',
    SUBMIT: '=',
    RESUME_ACCEPTED: '=',
    REJECTED_conflict: '=',
    REJECTED_other: '=',
    RESET: 'no_gate',
  },
  failed: {
    DISCOVERED_same: '=',
    DISCOVERED_diff: 'open',
    GATE_CLEARED: 'superseded',
    EDIT: 'failed',
    SUBMIT: 'submitting',
    RESUME_ACCEPTED: '=',
    REJECTED_conflict: '=',
    REJECTED_other: '=',
    RESET: 'no_gate',
  },
}

describe('gateReducer transition table', () => {
  for (const [stateName, row] of Object.entries(TABLE)) {
    for (const [eventName, expected] of Object.entries(row)) {
      it(`${stateName} x ${eventName} -> ${expected === '=' ? 'no-op' : expected}`, () => {
        const state = STATES[stateName] as GateMachineState
        const event = (EVENTS[eventName] as (s: GateMachineState) => GateEvent)(state)
        const next = gateReducer(state, event)
        if (expected === '=') {
          // No-ops MUST return the identical reference (effect-loop safety).
          expect(next).toBe(state)
        } else {
          expect(next.tag).toBe(expected)
        }
      })
    }
  }
})

describe('gateReducer semantics beyond tags', () => {
  it('a different interrupt_id is a NEW gate instance with a fresh draft', () => {
    const open = gateReducer(STATES['open'] as GateMachineState, {
      type: 'EDIT',
      patch: { prompt: { system: 'EDITED SYSTEM' } },
    })
    const next = gateReducer(open, { type: 'GATE_DISCOVERED', gate: otherGate })
    expect(next).toMatchObject({
      tag: 'open',
      gate: { interrupt_id: 'int-2' },
      dirty: false,
    })
    if (next.tag !== 'open') throw new Error('unreachable')
    expect(next.draft.prompt?.system).toBe('You are the planning agent.')
  })

  it('EDIT merges prompt patches and tracks dirty against the payload original', () => {
    let state = gateReducer(STATES['open'] as GateMachineState, {
      type: 'EDIT',
      patch: { prompt: { system: 'EDITED SYSTEM' } },
    })
    if (state.tag !== 'open') throw new Error('expected open')
    expect(state.dirty).toBe(true)
    expect(state.draft.prompt).toEqual({
      system: 'EDITED SYSTEM',
      user: 'Plan load coverage for APEX-101.',
      application: 'Checkout must preserve carts during payment retries.',
    })
    // Reverting the edit un-dirties the draft.
    state = gateReducer(state, {
      type: 'EDIT',
      patch: { prompt: { system: 'You are the planning agent.' } },
    })
    if (state.tag !== 'open') throw new Error('expected open')
    expect(state.dirty).toBe(false)
    // Prompt-review notes are Additional Context and count as edited run-scoped prompt state.
    state = gateReducer(state, { type: 'EDIT', patch: { note: 'looks fine' } })
    if (state.tag !== 'open') throw new Error('expected open')
    expect(state.dirty).toBe(true)
    expect(state.draft.note).toBe('looks fine')
  })

  it('rejected(!conflict) preserves the draft and the action for [Retry]', () => {
    const submitting = gateReducer(
      {
        tag: 'open',
        gate: promptGate,
        draft: { prompt: { system: 'EDITED', user: 'kept' } },
        dirty: true,
      },
      { type: 'SUBMIT', action: 'modify' },
    )
    const failed = gateReducer(submitting, {
      type: 'RESUME_REJECTED',
      error: failure,
      conflict: false,
    })
    expect(failed).toMatchObject({
      tag: 'failed',
      action: 'modify',
      draft: { prompt: { system: 'EDITED', user: 'kept' } },
      error: failure,
    })
    // Retry: SUBMIT from failed re-enters submitting with the SAME draft.
    const retried = gateReducer(failed, { type: 'SUBMIT', action: 'modify' })
    expect(retried).toMatchObject({
      tag: 'submitting',
      action: 'modify',
      draft: { prompt: { system: 'EDITED', user: 'kept' } },
    })
  })

  it('accepted modify/discuss/revise awaits the agent; accepted approve clears', () => {
    const modify = gateReducer(
      {
        tag: 'submitting',
        gate: promptGate,
        action: 'modify',
        draft: initialDraftFor(promptGate),
      },
      { type: 'RESUME_ACCEPTED' },
    )
    expect(modify).toMatchObject({ tag: 'awaiting_agent', action: 'modify' })
    const discuss = gateReducer(STATES['submitting_discuss'] as GateMachineState, {
      type: 'RESUME_ACCEPTED',
    })
    expect(discuss).toMatchObject({ tag: 'awaiting_agent', action: 'discuss' })
    const retry = gateReducer(
      { tag: 'submitting', gate: retryGate, action: 'retry', draft: {} },
      { type: 'RESUME_ACCEPTED' },
    )
    expect(retry).toMatchObject({ tag: 'awaiting_agent', action: 'retry' })
    const approve = gateReducer(STATES['submitting_approve'] as GateMachineState, {
      type: 'RESUME_ACCEPTED',
    })
    expect(approve.tag).toBe('no_gate')
  })

  it('clears awaiting_agent only when a refreshed run settled without another gate', () => {
    const awaiting = STATES['awaiting_agent'] as GateMachineState
    expect(gateReducer(awaiting, { type: 'GATE_CLEARED' })).toBe(awaiting)
    expect(gateReducer(awaiting, { type: 'GATE_CLEARED', settled: true })).toEqual({
      tag: 'no_gate',
    })
  })

  it('isPromptDirty compares against the payload original', () => {
    expect(isPromptDirty(promptGate, initialDraftFor(promptGate))).toBe(false)
    expect(isPromptDirty(promptGate, { prompt: { system: 'X', user: 'kept' } })).toBe(true)
    expect(isPromptDirty(phaseGate, {})).toBe(false)
  })
})

describe('buildResumeBody', () => {
  it('modify carries the draft prompt (and note when present)', () => {
    expect(
      buildResumeBody('modify', {
        prompt: { system: 'EDITED', user: 'kept' },
        note: 'tightened scope',
      }),
    ).toEqual({
      action: 'modify',
      prompt: { system: 'EDITED', user: 'kept' },
      note: 'tightened scope',
    })
  })

  it('discuss carries only the message; revise only the instructions', () => {
    expect(
      buildResumeBody('discuss', { message: 'why no auth flows?', instructions: 'ignored' }),
    ).toEqual({ action: 'discuss', message: 'why no auth flows?' })
    expect(
      buildResumeBody('revise', { instructions: 'add auth flows', message: 'ignored' }),
    ).toEqual({ action: 'revise', instructions: 'add auth flows' })
  })

  it('approve/skip_phase/abort send the bare action', () => {
    expect(buildResumeBody('approve', { prompt: { system: 'a', user: 'b' } })).toEqual({
      action: 'approve',
    })
    expect(buildResumeBody('skip_phase', {})).toEqual({ action: 'skip_phase' })
    expect(buildResumeBody('abort', {})).toEqual({ action: 'abort' })
  })
})
