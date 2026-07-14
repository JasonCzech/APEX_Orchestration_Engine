/**
 * Contract tests: fixtures lifted from the backend's own test suite.
 *
 * Sources of every literal below (do not "fix" a failure here by loosening a
 * schema without checking the backend first):
 *   tests/unit/test_pipeline_graph.py   — gate payload assertions
 *     (test_gated_run_pauses_for_prompt_review_then_approve,
 *      test_modify_reinterrupts_with_edited_prompt_then_approve,
 *      test_discuss_appends_dialogue_and_reinterrupts,
 *      test_unknown_action_reinterrupts_with_error,
 *      test_custom_events_streamed, test_subset_run_uses_thread_state...)
 *   tests/unit/test_execution_phase.py  — engine_poll + execution state
 *     (test_engine_poll_custom_events_streamed,
 *      test_e2e_all_auto_full_pipeline_runs_engine_and_reports,
 *      test_gated_output_review_opens_after_collect_with_summary)
 *   plus the emit_event / payload-builder call sites those tests assert against
 *   (src/apex/graphs/pipeline/{gates,phase_subgraph,execution_phase,graph}.py).
 *
 * FOLLOW-UP: these fixtures are hand-lifted. Replace with a generated-fixtures
 * pipeline (backend tests dump asserted payloads to JSON; this suite parses
 * them) so divergence is caught mechanically.
 */
import { describe, expect, it } from "vitest";

import {
  AgentErrorEventSchema,
  AgentMessageEventSchema,
  EnginePollErrorEventSchema,
  EnginePollEventSchema,
  GateDecisionSchema,
  GateInterruptPayloadSchema,
  GateOpenedEventSchema,
  PhaseResultEntrySchema,
  PhaseReviewDecisionSchema,
  PhaseReviewPayloadSchema,
  PhaseStatusEventSchema,
  PipelineEventSchema,
  PipelineStateSchema,
  PlanResolvedEventSchema,
  PromptReviewDecisionSchema,
  PromptReviewPayloadSchema,
  ToolCallEventSchema,
  parseGateInterrupt,
  parsePipelineEvent,
} from "../src/index";

// ── event fixtures (emit_event call sites; asserted in test_custom_events_streamed,
//    test_subset_run_uses_thread_state_and_increments_attempt,
//    test_engine_poll_custom_events_streamed) ───────────────────────────────────

const planResolved = {
  schema_version: 1,
  type: "plan_resolved",
  phases: ["story_analysis"],
};

const phaseStatusRunning = {
  schema_version: 1,
  type: "phase_status",
  phase: "story_analysis",
  status: "running",
  attempt: 1,
};

const phaseStatusSucceeded = {
  schema_version: 1,
  type: "phase_status",
  phase: "story_analysis",
  status: "succeeded",
  attempt: 1,
};

const gateOpened = {
  schema_version: 1,
  type: "gate_opened",
  gate: "prompt_review",
  phase: "story_analysis",
  attempt: 1,
};

// id/tool literals asserted in test_subset_run (attempt 2 re-run).
const toolCall = {
  schema_version: 1,
  type: "tool_call",
  phase: "test_planning",
  id: "test_planning-a2-r0-stub-lookup",
  tool: "test_planning.stub_lookup",
  status: "ok",
};

const agentMessage = {
  schema_version: 1,
  type: "agent_message",
  phase: "story_analysis",
  model: "claude-sonnet-4-5",
  chars: 842,
};

const agentError = {
  schema_version: 1,
  type: "agent_error",
  phase: "story_analysis",
  error: "provider request timed out",
};

const enginePollError = {
  schema_version: 1,
  type: "engine_poll_error",
  phase: "execution",
  attempt: 1,
  error: "provider status request timed out",
  consecutive_errors: 2,
};

// Field set from execution_phase._poll_event; live_stats keys asserted exactly
// in test_engine_poll_custom_events_streamed.
const enginePollRunning = {
  schema_version: 1,
  type: "engine_poll",
  phase: "execution",
  attempt: 1,
  engine: "sim",
  external_run_id: "sim-0f3a9c1e2b7d4e61",
  status: "running",
  progress_pct: 37.5,
  live_stats: { vusers: 5.0, tps: 24.6, error_rate: 0.0, p95_ms: 181.0 },
};

const enginePollCompleted = {
  ...enginePollRunning,
  status: "completed",
  progress_pct: 100.0,
  live_stats: { vusers: 0.0, tps: 24.6, error_rate: 0.0, p95_ms: 181.0 },
};

describe("custom event schemas", () => {
  it.each([
    ["plan_resolved", PlanResolvedEventSchema, planResolved],
    ["phase_status running", PhaseStatusEventSchema, phaseStatusRunning],
    ["phase_status succeeded", PhaseStatusEventSchema, phaseStatusSucceeded],
    ["gate_opened", GateOpenedEventSchema, gateOpened],
    ["tool_call", ToolCallEventSchema, toolCall],
    ["agent_message", AgentMessageEventSchema, agentMessage],
    ["agent_error", AgentErrorEventSchema, agentError],
    ["engine_poll_error", EnginePollErrorEventSchema, enginePollError],
    ["engine_poll running", EnginePollEventSchema, enginePollRunning],
    ["engine_poll completed", EnginePollEventSchema, enginePollCompleted],
  ] as const)("parses the backend fixture: %s", (_name, schema, fixture) => {
    expect(schema.safeParse(fixture).success).toBe(true);
    // every fixture also routes through the union
    expect(PipelineEventSchema.safeParse(fixture).success).toBe(true);
  });

  it("tolerates unknown fields (passthrough) and preserves them", () => {
    const parsed = PipelineEventSchema.parse({ ...phaseStatusRunning, trace_id: "abc123" });
    expect((parsed as Record<string, unknown>)["trace_id"]).toBe("abc123");
  });

  it("rejects a schema_version other than 1", () => {
    expect(
      PhaseStatusEventSchema.safeParse({ ...phaseStatusRunning, schema_version: 2 }).success,
    ).toBe(false);
  });

  it("rejects mutated bad fixtures", () => {
    // missing required field
    const { phases: _phases, ...planWithoutPhases } = planResolved;
    expect(PlanResolvedEventSchema.safeParse(planWithoutPhases).success).toBe(false);
    // enum drift: unknown phase status
    expect(
      PhaseStatusEventSchema.safeParse({ ...phaseStatusRunning, status: "exploded" }).success,
    ).toBe(false);
    // wrong primitive type
    expect(ToolCallEventSchema.safeParse({ ...toolCall, status: 200 }).success).toBe(false);
    // engine_poll live_stats must carry all four keys
    expect(
      EnginePollEventSchema.safeParse({
        ...enginePollRunning,
        live_stats: { vusers: 5.0, tps: 24.6 },
      }).success,
    ).toBe(false);
  });

  it("union rejects unknown event types and routes to the drift hook", () => {
    const unknown = { schema_version: 1, type: "engine_warmup", phase: "execution" };
    expect(PipelineEventSchema.safeParse(unknown).success).toBe(false);

    const drifts: unknown[] = [];
    const parsed = parsePipelineEvent(unknown, (drift) => drifts.push(drift.data));
    expect(parsed).toBeNull();
    expect(drifts).toEqual([unknown]);

    // known event: no drift, typed result
    const ok = parsePipelineEvent(planResolved, () => {
      throw new Error("drift hook must not fire for a valid event");
    });
    expect(ok?.type).toBe("plan_resolved");
  });
});

// ── interrupt payload fixtures (gates.build_*; asserted in
//    test_gated_run_pauses..., test_modify_reinterrupts...,
//    test_unknown_action..., test_discuss..., test_gated_output_review...) ─────

const promptReview = {
  schema_version: 1,
  kind: "prompt_review",
  phase: "story_analysis",
  prompt: {
    system: "You are the story_analysis agent for an APEX load-testing pipeline.",
    user: "Title: Demo\nRequest: r",
    application: "Checkout app requirements.",
    source: { origin: "catalog", ref: "phase/story_analysis@builtin" },
  },
  context_packets: [],
  tools: ["story_analysis.stub_lookup"],
  editable: true,
  actions: ["approve", "modify", "skip_phase", "abort"],
};

// after a modify resume (test_modify_reinterrupts_with_edited_prompt_then_approve)
const promptReviewAfterEdit = {
  ...promptReview,
  prompt: {
    system: "You are edited.",
    user: "Title: Demo\nRequest: (no request provided)",
    application: "Edited checkout requirements.",
    source: { origin: "gate_edit", ref: "phase/story_analysis@builtin" },
  },
};

// re-interrupt after a bad action (test_unknown_action_reinterrupts_with_error)
const promptReviewWithError = {
  ...promptReview,
  error: "unknown action 'bogus'; expected one of ['abort', 'approve', 'modify', 'skip_phase']",
};

// phase_review mid-discuss (test_discuss_appends_dialogue_and_reinterrupts)
const phaseReview = {
  schema_version: 1,
  kind: "phase_review",
  phase: "story_analysis",
  summary: "[story_analysis] stub result for 'Demo': (no request provided)",
  result_preview: {
    summary: "[story_analysis] stub result for 'Demo': (no request provided)",
    reasoning_digest: "Deterministic stub reasoning for story_analysis (attempt 1, revision 0).",
  },
  artifacts: [],
  warnings: [],
  dialogue_tail: [
    {
      id: "story_analysis-a1-d0-operator",
      phase: "story_analysis",
      role: "operator",
      content: "why this scope?",
      at: "2026-06-11T12:00:00.000000+00:00",
    },
    {
      id: "story_analysis-a1-d1-agent",
      phase: "story_analysis",
      role: "agent",
      content: "[story_analysis agent stub] acknowledged: why this scope?",
      at: "2026-06-11T12:00:00.000001+00:00",
    },
  ],
  actions: ["approve", "revise", "discuss", "abort"],
};

// execution output review (test_gated_output_review_opens_after_collect_with_summary):
// reasoning_digest is null (engine spine never sets it), artifacts carry the
// engine_results preview ({id, kind, name} built in output_gate; the sim engine
// names its results artifact "results.json").
const phaseReviewExecution = {
  schema_version: 1,
  kind: "phase_review",
  phase: "execution",
  summary:
    "Engine run sim-0f3a9c1e2b7d4e61 (sim) completed; SLA passed. " +
    "KPIs: error_rate=0, p95_ms=181, tps_avg=24.6, vusers_peak=5",
  result_preview: {
    summary:
      "Engine run sim-0f3a9c1e2b7d4e61 (sim) completed; SLA passed. " +
      "KPIs: error_rate=0, p95_ms=181, tps_avg=24.6, vusers_peak=5",
    reasoning_digest: null,
  },
  artifacts: [
    { id: "execution-a1-engine-artifact-0", kind: "engine_results", name: "results.json" },
  ],
  warnings: [],
  dialogue_tail: [],
  actions: ["approve", "revise", "discuss", "abort"],
};

describe("gate interrupt payload schemas", () => {
  it.each([
    ["prompt_review", promptReview],
    ["prompt_review after gate edit", promptReviewAfterEdit],
    ["prompt_review with error", promptReviewWithError],
    ["phase_review with dialogue", phaseReview],
    ["phase_review execution", phaseReviewExecution],
  ] as const)("parses the backend fixture: %s", (_name, fixture) => {
    expect(GateInterruptPayloadSchema.safeParse(fixture).success).toBe(true);
  });

  it("keeps the exact action arrays the backend advertises", () => {
    const prompt = PromptReviewPayloadSchema.parse(promptReview);
    expect(prompt.actions).toEqual(["approve", "modify", "skip_phase", "abort"]);
    const phase = PhaseReviewPayloadSchema.parse(phaseReview);
    expect(phase.actions).toEqual(["approve", "revise", "discuss", "abort"]);
  });

  it("rejects mutated bad fixtures", () => {
    // missing editable flag
    const { editable: _editable, ...withoutEditable } = promptReview;
    expect(PromptReviewPayloadSchema.safeParse(withoutEditable).success).toBe(false);
    // prompt.source.origin outside the known origins
    expect(
      PromptReviewPayloadSchema.safeParse({
        ...promptReview,
        prompt: { ...promptReview.prompt, source: { origin: "wikipedia", ref: null } },
      }).success,
    ).toBe(false);
    // dialogue_tail entry with an unknown role
    expect(
      PhaseReviewPayloadSchema.safeParse({
        ...phaseReview,
        dialogue_tail: [{ id: "x", role: "narrator", content: "hi" }],
      }).success,
    ).toBe(false);
    // schema_version drift
    expect(
      PhaseReviewPayloadSchema.safeParse({ ...phaseReview, schema_version: 2 }).success,
    ).toBe(false);
  });

  it("union rejects unknown kinds and routes to the drift hook", () => {
    const unknown = { schema_version: 1, kind: "budget_review", phase: "execution" };
    expect(GateInterruptPayloadSchema.safeParse(unknown).success).toBe(false);
    const drifts: unknown[] = [];
    expect(parseGateInterrupt(unknown, (drift) => drifts.push(drift.data))).toBeNull();
    expect(drifts).toEqual([unknown]);
    expect(parseGateInterrupt(promptReview)?.kind).toBe("prompt_review");
  });
});

// ── resume bodies (exact dicts sent via Command(resume=...) in the backend tests) ──

describe("gate decision (resume body) schemas", () => {
  it.each([
    [{ action: "approve" }],
    [{ action: "modify", prompt: { system: "You are edited.", application: "App edit." } }],
    [{ action: "skip_phase" }],
    [{ action: "abort" }],
  ] as const)("prompt_review accepts backend-tested resume %j", (decision) => {
    expect(PromptReviewDecisionSchema.safeParse(decision).success).toBe(true);
  });

  it.each([
    [{ action: "approve" }],
    [{ action: "revise", instructions: "add latency numbers" }],
    [{ action: "discuss", message: "why this scope?" }],
    [{ action: "abort" }],
  ] as const)("phase_review accepts backend-tested resume %j", (decision) => {
    expect(PhaseReviewDecisionSchema.safeParse(decision).success).toBe(true);
  });

  it("rejects unknown or missing actions", () => {
    expect(GateDecisionSchema.safeParse({ action: "bogus" }).success).toBe(false);
    expect(GateDecisionSchema.safeParse({}).success).toBe(false);
    expect(PromptReviewDecisionSchema.safeParse({ action: "revise" }).success).toBe(false);
    expect(PhaseReviewDecisionSchema.safeParse({ action: "skip_phase" }).success).toBe(false);
  });
});

// ── thread-state mirror (asserted shapes from test_e2e_all_auto_full_pipeline...) ──

const executionEntry = {
  phase: "execution",
  status: "succeeded",
  attempt: 1,
  started_at: "2026-06-11T12:00:00.000000+00:00",
  ended_at: "2026-06-11T12:00:01.250000+00:00",
  duration_s: 1.25,
  summary:
    "Engine run sim-0f3a9c1e2b7d4e61 (sim) completed; SLA passed. " +
    "KPIs: error_rate=0, p95_ms=181, tps_avg=24.6, vusers_peak=4",
  reasoning_digest: null,
  transcript_ref: {
    id: "execution-a1-transcript",
    kind: "transcript",
    name: "execution transcript (attempt 1)",
    uri: "memory://transcripts/thread-1/execution/attempt-1",
    media_type: "text/plain",
    summary: null,
    created_at: "2026-06-11T12:00:01.250000+00:00",
  },
  artifact_ids: ["execution-a1-engine-artifact-0", "execution-a1-transcript"],
  warnings: [],
  errors: [],
  approvals: [],
  tool_calls: [],
  resolved_prompt_source: { origin: "catalog", ref: "phase/execution@builtin", editor: null },
  resolved_prompt: {
    system: "You are the execution agent for an APEX load-testing pipeline.",
    user: "Title: Demo\nRequest: Load test the checkout flow",
  },
  load_test_spec: {
    idempotency_key: "exec-e2e-execution-a1",
    title: "Demo load test",
    script_refs: ["stub://scripts/exec-e2e/script_scenario-a1.jmx"],
    vusers: 4,
    ramp_s: 1.0,
    duration_s: 0.2,
    slas: { p95_ms: 500.0, error_rate: 0.05 },
    target_environment: null,
  },
  engine: "sim",
  engine_connection_id: null,
  engine_options: {},
  engine_handle: {
    engine: "sim",
    connection_id: null,
    external_run_id: "sim-0f3a9c1e2b7d4e61",
    idempotency_key: "exec-e2e-execution-a1",
    extras: {},
  },
  engine_started_at: "2026-06-11T12:00:00.100000+00:00",
  engine_poll_last: {
    at: "2026-06-11T12:00:01.200000+00:00",
    status: "completed",
    progress_pct: 100.0,
    live_stats: { vusers: 0.0, tps: 24.6, error_rate: 0.0, p95_ms: 181.0 },
  },
  engine_poll_count: 3,
  test_summary: {
    engine: "sim",
    passed: true,
    kpis: { tps_avg: 24.6, p95_ms: 181.0, error_rate: 0.0, vusers_peak: 4.0 },
    sla_breaches: [],
    notes: null,
  },
};

const pipelineState = {
  title: "Demo",
  request: "Load test the checkout flow",
  phases_plan: ["script_scenario", "execution"],
  current_phase: "execution",
  run_aborted: false,
  run_config: {
    project_id: "proj-alpha",
    engine: "sim",
    connections: { work_tracking: "conn-work" },
    limits: { max_revise_loops: 3 },
  },
  phase_results: { execution: executionEntry },
  artifacts: [
    {
      id: "execution-a1-engine-artifact-0",
      kind: "engine_results",
      name: "results.json",
      uri: "memory://engine-runs/sim-0f3a9c1e2b7d4e61/results.json",
      media_type: "application/json",
      summary: "Simulated engine results for sim-0f3a9c1e2b7d4e61",
      created_at: "2026-06-11T12:00:01.240000+00:00",
    },
  ],
  dialogue: [
    {
      id: "story_analysis-a1-d0-operator",
      phase: "story_analysis",
      role: "operator",
      content: "why this scope?",
      at: "2026-06-11T12:00:00.000000+00:00",
    },
  ],
  context_packets: [],
  engine_handle: {
    engine: "sim",
    connection_id: null,
    external_run_id: "sim-0f3a9c1e2b7d4e61",
    idempotency_key: "exec-e2e-execution-a1",
    extras: {},
  },
};

describe("thread-state mirror schemas", () => {
  it("parses a full execution phase entry incl. extra keys", () => {
    const parsed = PhaseResultEntrySchema.parse(executionEntry);
    expect(parsed.load_test_spec?.idempotency_key).toBe("exec-e2e-execution-a1");
    expect(parsed.test_summary?.passed).toBe(true);
    expect(parsed.engine_poll_last?.status).toBe("completed");
  });

  it("parses the full thread state", () => {
    const parsed = PipelineStateSchema.parse(pipelineState);
    expect(parsed.engine_handle?.external_run_id).toBe("sim-0f3a9c1e2b7d4e61");
    expect(parsed.phase_results?.["execution"]?.status).toBe("succeeded");
    expect(parsed.run_config?.["connections"]).toEqual({ work_tracking: "conn-work" });
  });

  it("stays lenient: partial mid-run entries and unknown keys still parse", () => {
    // plan_resolver seed (PhaseResult.as_state of a pending phase) minus extras
    expect(
      PhaseResultEntrySchema.safeParse({ phase: "reporting", status: "pending", attempt: 1 })
        .success,
    ).toBe(true);
    // future backend additions ride through
    const parsed = PhaseResultEntrySchema.parse({
      ...executionEntry,
      cost_usd: 0.42,
    });
    expect((parsed as Record<string, unknown>)["cost_usd"]).toBe(0.42);
  });

  it("still rejects values that contradict the contract", () => {
    expect(
      PhaseResultEntrySchema.safeParse({ ...executionEntry, status: "exploded" }).success,
    ).toBe(false);
    expect(
      PipelineStateSchema.safeParse({ ...pipelineState, run_aborted: "yes" }).success,
    ).toBe(false);
  });
});
