/**
 * Lenient mirrors of the pipeline thread-state slices the dashboard reads.
 *
 * AUTHORITATIVE SOURCE:
 *   src/apex/domain/pipeline.py            ArtifactRef, ApprovalRecord, ToolCallRecord,
 *                                          DialogueEntry, ContextPacket, EngineHandle,
 *                                          ResolvedPromptSource, PhaseResult
 *   src/apex/domain/integrations.py        LoadTestSpec, TestResultSummary
 *   src/apex/graphs/pipeline/state.py      PipelineState channels
 *   src/apex/graphs/pipeline/phase_subgraph.py + execution_phase.py
 *                                          extra phase-entry keys written by nodes
 *                                          (resolved_prompt, load_test_spec, test_summary,
 *                                          engine_*, revise_*)
 *
 * Policy: these schemas MIRROR state, they do not gate it. Everything is
 * `.passthrough()` and almost every field is optional/nullish, so a partially
 * populated entry (mid-run, mid-merge, or seeded) still parses. Only stable
 * identifiers and semantically load-bearing fields are required. The strict
 * boundary contracts live in events.ts / interrupts.ts.
 */
import { z } from "zod";

import {
  EngineRunPhaseSchema,
  GateNameSchema,
  LiveStatsSchema,
  PhaseNameSchema,
  PhaseStatusSchema,
} from "./events";

/** apex.domain.pipeline.ResolvedPromptSource.origin */
export const PromptOriginSchema = z.enum([
  "catalog",
  "assistant_pin",
  "run_override",
  "gate_edit",
]);
export type PromptOrigin = z.infer<typeof PromptOriginSchema>;

export const ArtifactRefSchema = z
  .object({
    id: z.string(),
    kind: z.string().optional(),
    name: z.string().optional(),
    uri: z.string().optional(),
    media_type: z.string().optional(),
    summary: z.string().nullish(),
    created_at: z.string().optional(),
  })
  .passthrough();
export type ArtifactRef = z.infer<typeof ArtifactRefSchema>;

export const ApprovalRecordSchema = z
  .object({
    id: z.string(),
    gate: GateNameSchema.optional(),
    action: z.string().optional(),
    actor: z.string().optional(),
    at: z.string().optional(),
    note: z.string().nullish(),
  })
  .passthrough();
export type ApprovalRecord = z.infer<typeof ApprovalRecordSchema>;

export const ToolCallRecordSchema = z
  .object({
    id: z.string(),
    tool: z.string().optional(),
    args_preview: z.record(z.unknown()).optional(),
    status: z.enum(["ok", "error"]).optional(),
    duration_ms: z.number().int().nullish(),
    error: z.string().nullish(),
    at: z.string().optional(),
  })
  .passthrough();
export type ToolCallRecord = z.infer<typeof ToolCallRecordSchema>;

export const DialogueEntrySchema = z
  .object({
    id: z.string(),
    phase: PhaseNameSchema.optional(),
    role: z.enum(["operator", "agent"]),
    content: z.string(),
    at: z.string().optional(),
  })
  .passthrough();
export type DialogueEntry = z.infer<typeof DialogueEntrySchema>;

export const ContextPacketSchema = z
  .object({
    id: z.string(),
    source: z.string().optional(),
    title: z.string().optional(),
    summary: z.string().nullish(),
    ref: z.string().nullish(),
  })
  .passthrough();
export type ContextPacket = z.infer<typeof ContextPacketSchema>;

export const EngineHandleSchema = z
  .object({
    engine: z.string(),
    connection_id: z.string().nullish(),
    external_run_id: z.string().nullish(),
    idempotency_key: z.string().optional(),
    extras: z.record(z.string()).optional(),
  })
  .passthrough();
export type EngineHandle = z.infer<typeof EngineHandleSchema>;

export const ResolvedPromptSourceSchema = z
  .object({
    origin: PromptOriginSchema.optional(),
    ref: z.string().nullish(),
    editor: z.string().nullish(),
  })
  .passthrough();
export type ResolvedPromptSource = z.infer<typeof ResolvedPromptSourceSchema>;

/** Extra phase-entry key written by prepare/prompt_gate: {system, user}. */
export const ResolvedPromptSchema = z
  .object({
    system: z.string().nullish(),
    user: z.string().nullish(),
  })
  .passthrough();
export type ResolvedPrompt = z.infer<typeof ResolvedPromptSchema>;

/** apex.domain.integrations.LoadTestSpec (extra entry key "load_test_spec"). */
export const LoadTestSpecSchema = z
  .object({
    idempotency_key: z.string().optional(),
    title: z.string().optional(),
    script_refs: z.array(z.string()).optional(),
    vusers: z.number().optional(),
    ramp_s: z.number().optional(),
    duration_s: z.number().optional(),
    slas: z.record(z.number()).optional(),
    target_environment: z.string().nullish(),
  })
  .passthrough();
export type LoadTestSpec = z.infer<typeof LoadTestSpecSchema>;

/** apex.domain.integrations.TestResultSummary (extra entry key "test_summary"). */
export const TestResultSummarySchema = z
  .object({
    engine: z.string().optional(),
    passed: z.boolean(),
    kpis: z.record(z.number()).optional(),
    sla_breaches: z.array(z.string()).optional(),
    notes: z.string().nullish(),
  })
  .passthrough();
export type TestResultSummary = z.infer<typeof TestResultSummarySchema>;

/** Compact rolling poll sample (execution_phase.py `_poll_sample`). */
export const EnginePollSampleSchema = z
  .object({
    at: z.string().optional(),
    status: EngineRunPhaseSchema.optional(),
    progress_pct: z.number().optional(),
    live_stats: LiveStatsSchema.optional(),
    message: z.string().nullish(),
  })
  .passthrough();
export type EnginePollSample = z.infer<typeof EnginePollSampleSchema>;

/**
 * One phase_results entry: the PhaseResult model dump PLUS the extra keys the
 * gate/agent/engine nodes merge in (none of which exist on the pydantic model).
 */
export const PhaseResultEntrySchema = z
  .object({
    // PhaseResult model fields
    phase: PhaseNameSchema.optional(),
    status: PhaseStatusSchema.optional(),
    attempt: z.number().int().optional(),
    started_at: z.string().nullish(),
    ended_at: z.string().nullish(),
    duration_s: z.number().nullish(),
    summary: z.string().nullish(),
    reasoning_digest: z.string().nullish(),
    transcript_ref: ArtifactRefSchema.nullish(),
    artifact_ids: z.array(z.string()).optional(),
    warnings: z.array(z.string()).optional(),
    errors: z.array(z.string()).optional(),
    approvals: z.array(ApprovalRecordSchema).optional(),
    tool_calls: z.array(ToolCallRecordSchema).optional(),
    resolved_prompt_source: ResolvedPromptSourceSchema.nullish(),
    // Extra keys: prompt resolution + revise loop (phase_subgraph.py)
    resolved_prompt: ResolvedPromptSchema.optional(),
    revise_instructions: z.string().nullish(),
    revise_count: z.number().int().optional(),
    // Extra keys: script_scenario output + execution engine spine (execution_phase.py)
    load_test_spec: LoadTestSpecSchema.optional(),
    test_summary: TestResultSummarySchema.optional(),
    engine: z.string().optional(),
    engine_connection_id: z.string().nullish(),
    engine_options: z.record(z.unknown()).optional(),
    engine_handle: EngineHandleSchema.optional(),
    engine_started_at: z.string().optional(),
    engine_poll_last: EnginePollSampleSchema.optional(),
    engine_poll_count: z.number().int().optional(),
  })
  .passthrough();
export type PhaseResultEntry = z.infer<typeof PhaseResultEntrySchema>;

/** apex.graphs.pipeline.state.PipelineState (thread values from the SDK). */
export const PipelineStateSchema = z
  .object({
    title: z.string().optional(),
    request: z.string().optional(),
    phases_plan: z.array(PhaseNameSchema).optional(),
    current_phase: PhaseNameSchema.nullish(),
    run_aborted: z.boolean().optional(),
    phase_results: z.record(PhaseResultEntrySchema).optional(),
    artifacts: z.array(ArtifactRefSchema).optional(),
    dialogue: z.array(DialogueEntrySchema).optional(),
    context_packets: z.array(ContextPacketSchema).optional(),
    engine_handle: EngineHandleSchema.nullish(),
  })
  .passthrough();
export type PipelineState = z.infer<typeof PipelineStateSchema>;
