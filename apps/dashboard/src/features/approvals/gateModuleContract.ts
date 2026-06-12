/**
 * The GateModule integration contract between the approvals inbox (plan UX
 * 2.b, this folder) and the shared gate module (plan 2.a, src/hitl). The
 * TYPES are canonical in src/hitl/GateModule.tsx — this file re-exports them
 * and records how the inbox uses the surface (src/hitl's doc comments point
 * here for the consumer side).
 *
 * Usage (ApprovalsInboxPage GatePreview):
 *   <GateModule
 *     threadId={detail.thread_id}
 *     interrupt={interrupt}          // from useThreadState().interrupts
 *     compact                       // inbox preview density
 *     onOutcome={...}                // terminal notification, once per instance
 *     handleRef={gateHandleRef}      // keyboard layer (a/m/s/x + Enter)
 *   />
 *   keyed by interrupt.interrupt_id — a discuss/revise re-interrupt mints a
 *   new id, remounting a fresh module (and re-arming onOutcome).
 *
 * Inbox semantics on onOutcome:
 *   {type:'resumed', action} with action approve/modify/skip_phase/abort
 *     -> the gate is settled HERE: gray the queue row ('actioned'),
 *        auto-advance the selection to the next open gate.
 *   {type:'resumed', action} with action discuss/revise
 *     -> NOT terminal for the queue: the gate reopens (awaiting_agent ->
 *        new interrupt) — selection stays put.
 *   {type:'superseded'}
 *     -> actioned elsewhere (409 CAS loss or interrupt vanished): gray the
 *        row ('actioned elsewhere'), auto-advance.
 *
 * Keyboard delegation (handleRef):
 *   a/s/x -> invoke('approve' | 'skip_phase' | 'abort')  — submit decisions
 *   m     -> invoke('modify')                            — modify-FOCUS only
 *   Enter -> focus()
 *   All gated on isActionable() (machine 'open'); invoke() additionally
 *   no-ops (returns false) when the gate kind/payload does not offer the
 *   action. The resume itself stays pessimistic inside the machine.
 */
export type { GateModuleHandle, GateModuleProps, GateOutcome } from '@/hitl/GateModule'
export type { GateAction } from '@/hitl/gateMachine'
