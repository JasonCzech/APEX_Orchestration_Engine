/**
 * Zod schemas for the pipeline's custom SSE stream events (stream_mode="custom").
 *
 * AUTHORITATIVE SOURCE — the backend emit_event call sites:
 *   src/apex/graphs/pipeline/graph.py            plan_resolved
 *   src/apex/graphs/pipeline/phase_subgraph.py   phase_status, gate_opened, tool_call
 *   src/apex/graphs/pipeline/execution_phase.py  phase_status, engine_poll
 *
 * Forward-compatibility policy (schema_version 1):
 * - Unknown FIELDS on a known event type are tolerated (`.passthrough()`) and
 *   preserved on the parsed value: the backend may add fields without breaking
 *   deployed dashboards.
 * - Unknown event TYPES are rejected by the discriminated union. Callers must
 *   route the failure to a `reportSchemaDrift` hook (fail-loud in dev,
 *   tolerate-and-log in prod) instead of crashing the stream consumer — see
 *   `parsePipelineEvent`.
 * - A `schema_version` other than 1 is rejected: version bumps are breaking by
 *   definition and require a coordinated dashboard update.
 * - Enum widening (a new phase name, phase status, gate, or engine-run status)
 *   is also treated as drift: these enums drive routing and UI state machines,
 *   so silently accepting unknown members would render broken screens. The
 *   drift hook fires and the contract test against backend fixtures catches the
 *   divergence at build time.
 */
import { z } from "zod";

export const EVENT_SCHEMA_VERSION = 1;

/** The 7 pipeline phases, in canonical order (apex.domain.pipeline.PHASE_ORDER). */
export const PHASE_NAMES = [
  "story_analysis",
  "test_planning",
  "env_triage",
  "script_scenario",
  "execution",
  "reporting",
  "postmortem",
] as const;

export const PhaseNameSchema = z.enum(PHASE_NAMES);
export type PhaseName = z.infer<typeof PhaseNameSchema>;

/** apex.domain.pipeline.PhaseStatus */
export const PhaseStatusSchema = z.enum([
  "pending",
  "running",
  "awaiting_prompt_review",
  "awaiting_output_review",
  "succeeded",
  "failed",
  "skipped",
  "aborted",
]);
export type PhaseStatus = z.infer<typeof PhaseStatusSchema>;

/** apex.ports.execution_engine.EngineRunPhase */
export const EngineRunPhaseSchema = z.enum([
  "provisioning",
  "ready",
  "running",
  "stopping",
  "collecting",
  "completed",
  "failed",
  "aborted",
]);
export type EngineRunPhase = z.infer<typeof EngineRunPhaseSchema>;

/** The two HITL gates (apex.graphs.pipeline.gates). */
export const GateNameSchema = z.enum(["prompt_review", "phase_review"]);
export type GateName = z.infer<typeof GateNameSchema>;

/** apex.ports.execution_engine.LiveStats — always all four keys on the wire. */
export const LiveStatsSchema = z
  .object({
    vusers: z.number(),
    tps: z.number(),
    error_rate: z.number(),
    p95_ms: z.number(),
  })
  .passthrough();
export type LiveStats = z.infer<typeof LiveStatsSchema>;

const schemaVersion = z.literal(EVENT_SCHEMA_VERSION);

/** Emitted once per run by plan_resolver (graph.py). */
export const PlanResolvedEventSchema = z
  .object({
    schema_version: schemaVersion,
    type: z.literal("plan_resolved"),
    phases: z.array(PhaseNameSchema),
  })
  .passthrough();
export type PlanResolvedEvent = z.infer<typeof PlanResolvedEventSchema>;

/**
 * Emitted by prepare (status "running"), finalize (terminal status), and
 * engine_start (status "running") — phase_subgraph.py / execution_phase.py.
 */
export const PhaseStatusEventSchema = z
  .object({
    schema_version: schemaVersion,
    type: z.literal("phase_status"),
    phase: PhaseNameSchema,
    status: PhaseStatusSchema,
    attempt: z.number().int(),
  })
  .passthrough();
export type PhaseStatusEvent = z.infer<typeof PhaseStatusEventSchema>;

/**
 * Emitted by open_prompt_gate / open_output_gate just before the awaiting_*
 * status is checkpointed and the graph interrupts (phase_subgraph.py).
 */
export const GateOpenedEventSchema = z
  .object({
    schema_version: schemaVersion,
    type: z.literal("gate_opened"),
    gate: GateNameSchema,
    phase: PhaseNameSchema,
    attempt: z.number().int(),
  })
  .passthrough();
export type GateOpenedEvent = z.infer<typeof GateOpenedEventSchema>;

/** Emitted by the stub agent for each tool call (phase_subgraph.py). NO attempt field. */
export const ToolCallEventSchema = z
  .object({
    schema_version: schemaVersion,
    type: z.literal("tool_call"),
    phase: PhaseNameSchema,
    id: z.string(),
    tool: z.string(),
    status: z.enum(["ok", "error"]),
  })
  .passthrough();
export type ToolCallEvent = z.infer<typeof ToolCallEventSchema>;

/**
 * Emitted once by engine_start (initial tick) then once per poll cycle by
 * engine_poll (execution_phase.py `_poll_event`). `status` is the ENGINE run
 * phase (provisioning/running/completed/...), not the pipeline phase status.
 */
export const EnginePollEventSchema = z
  .object({
    schema_version: schemaVersion,
    type: z.literal("engine_poll"),
    phase: PhaseNameSchema,
    attempt: z.number().int(),
    engine: z.string(),
    external_run_id: z.string().nullable(),
    status: EngineRunPhaseSchema,
    progress_pct: z.number(),
    live_stats: LiveStatsSchema,
  })
  .passthrough();
export type EnginePollEvent = z.infer<typeof EnginePollEventSchema>;

/** Discriminated union over every custom event the pipeline emits. */
export const PipelineEventSchema = z.discriminatedUnion("type", [
  PlanResolvedEventSchema,
  PhaseStatusEventSchema,
  GateOpenedEventSchema,
  ToolCallEventSchema,
  EnginePollEventSchema,
]);
export type PipelineEvent = z.infer<typeof PipelineEventSchema>;

/** Hook signature for routing contract drift (unknown type / shape mismatch). */
export type SchemaDriftReporter = (drift: { data: unknown; error: z.ZodError }) => void;

/**
 * Boundary parser: returns the typed event, or null after routing the failure
 * to the drift hook. Callers decide the policy (throw in dev, log in prod).
 */
export function parsePipelineEvent(
  data: unknown,
  reportSchemaDrift?: SchemaDriftReporter,
): PipelineEvent | null {
  const result = PipelineEventSchema.safeParse(data);
  if (result.success) return result.data;
  reportSchemaDrift?.({ data, error: result.error });
  return null;
}
