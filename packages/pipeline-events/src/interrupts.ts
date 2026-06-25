/**
 * Zod schemas for HITL gate interrupt payloads and the resume (decision) bodies.
 *
 * AUTHORITATIVE SOURCE:
 *   src/apex/graphs/pipeline/gates.py
 *     build_prompt_review_payload / build_phase_review_payload (payload shapes,
 *     GATE_SCHEMA_VERSION, PROMPT_REVIEW_ACTIONS / PHASE_REVIEW_ACTIONS)
 *     parse_gate_decision (what the backend accepts as Command(resume={...}))
 *   src/apex/graphs/pipeline/phase_subgraph.py
 *     prompt_gate / output_gate (which decision keys each action consumes:
 *     modify -> prompt{system,user}, revise -> instructions, discuss -> message)
 *
 * Same forward-compat policy as events.ts: unknown fields tolerated via
 * `.passthrough()`, unknown `kind` rejected by the discriminated union (route
 * to reportSchemaDrift), `schema_version` !== 1 rejected.
 *
 * One deliberate leniency: the `actions` array is `z.array(z.string())`, not an
 * enum array. It drives a button list, so a newly added backend action should
 * not unrender the whole gate — the dashboard renders the known subset and can
 * report the unknown member. The action LITERALS for what the dashboard SENDS
 * are strict (the decision schemas below).
 */
import { z } from "zod";

import { PhaseNameSchema, type SchemaDriftReporter } from "./events";
import { DialogueEntrySchema, PromptOriginSchema } from "./state";

export const GATE_SCHEMA_VERSION = 1;

/** gates.PROMPT_REVIEW_ACTIONS — exact order is asserted by the contract test. */
export const PROMPT_REVIEW_ACTIONS = ["approve", "modify", "skip_phase", "abort"] as const;
/** gates.PHASE_REVIEW_ACTIONS — exact order is asserted by the contract test. */
export const PHASE_REVIEW_ACTIONS = ["approve", "revise", "discuss", "abort"] as const;

const schemaVersion = z.literal(GATE_SCHEMA_VERSION);

/**
 * prompt.source inside the prompt_review payload. The builder copies only
 * origin/ref (editor is dropped) and uses .get(), so both may be null.
 */
export const ReviewPromptSourceSchema = z
  .object({
    origin: PromptOriginSchema.nullable(),
    ref: z.string().nullable(),
  })
  .passthrough();
export type ReviewPromptSource = z.infer<typeof ReviewPromptSourceSchema>;

export const ReviewPromptSchema = z
  .object({
    system: z.string().nullable(),
    user: z.string().nullable(),
    application: z.string().nullable().optional(),
    source: ReviewPromptSourceSchema,
  })
  .passthrough();
export type ReviewPrompt = z.infer<typeof ReviewPromptSchema>;

/** Context-packet preview: builder uses .get() on every key, so all nullable. */
export const ContextPacketPreviewSchema = z
  .object({
    id: z.string().nullable(),
    source: z.string().nullable(),
    title: z.string().nullable(),
    summary: z.string().nullable(),
  })
  .passthrough();
export type ContextPacketPreview = z.infer<typeof ContextPacketPreviewSchema>;

export const PromptReviewPayloadSchema = z
  .object({
    schema_version: schemaVersion,
    kind: z.literal("prompt_review"),
    phase: PhaseNameSchema,
    prompt: ReviewPromptSchema,
    additional_context: z.string().optional(),
    context_packets: z.array(ContextPacketPreviewSchema),
    tools: z.array(z.string()),
    editable: z.boolean(),
    actions: z.array(z.string()),
    /** Present only when a prior resume was rejected (re-interrupt). */
    error: z.string().optional(),
  })
  .passthrough();
export type PromptReviewPayload = z.infer<typeof PromptReviewPayloadSchema>;

/** result_preview is {summary, reasoning_digest} today, but typed open. */
export const ResultPreviewSchema = z
  .object({
    summary: z.string().nullish(),
    reasoning_digest: z.string().nullish(),
  })
  .passthrough();
export type ResultPreview = z.infer<typeof ResultPreviewSchema>;

/** Artifact preview rows ({id, kind, name} built in output_gate). */
export const ArtifactPreviewSchema = z
  .object({
    id: z.string().nullable(),
    kind: z.string().nullable(),
    name: z.string().nullable(),
  })
  .passthrough();
export type ArtifactPreview = z.infer<typeof ArtifactPreviewSchema>;

export const PhaseReviewPayloadSchema = z
  .object({
    schema_version: schemaVersion,
    kind: z.literal("phase_review"),
    phase: PhaseNameSchema,
    summary: z.string().nullable(),
    result_preview: ResultPreviewSchema,
    artifacts: z.array(ArtifactPreviewSchema),
    warnings: z.array(z.string()),
    /** Last <=3 dialogue entries for this phase (DialogueEntry dumps). */
    dialogue_tail: z.array(DialogueEntrySchema),
    actions: z.array(z.string()),
    /** Present only when a prior resume was rejected (re-interrupt). */
    error: z.string().optional(),
  })
  .passthrough();
export type PhaseReviewPayload = z.infer<typeof PhaseReviewPayloadSchema>;

/** Discriminated union over both gate interrupt payloads. */
export const GateInterruptPayloadSchema = z.discriminatedUnion("kind", [
  PromptReviewPayloadSchema,
  PhaseReviewPayloadSchema,
]);
export type GateInterruptPayload = z.infer<typeof GateInterruptPayloadSchema>;

/**
 * Boundary parser mirroring parsePipelineEvent: typed payload or null after
 * routing the failure to the drift hook.
 */
export function parseGateInterrupt(
  data: unknown,
  reportSchemaDrift?: SchemaDriftReporter,
): GateInterruptPayload | null {
  const result = GateInterruptPayloadSchema.safeParse(data);
  if (result.success) return result.data;
  reportSchemaDrift?.({ data, error: result.error });
  return null;
}

// ── Resume bodies (what the dashboard SENDS as Command(resume={...})) ─────────
//
// parse_gate_decision passes extra keys through, so each schema models exactly
// the keys the gate nodes consume; `note` rides along for attribution UX.

/** Partial prompt edit: omitted keys keep the current value (prompt_gate). */
export const PromptEditSchema = z
  .object({
    system: z.string().optional(),
    user: z.string().optional(),
    application: z.string().optional(),
  })
  .passthrough();
export type PromptEdit = z.infer<typeof PromptEditSchema>;

export const ApproveActionSchema = z.object({
  action: z.literal("approve"),
  note: z.string().optional(),
});
export const ModifyActionSchema = z.object({
  action: z.literal("modify"),
  prompt: PromptEditSchema.optional(),
  note: z.string().optional(),
});
export const SkipPhaseActionSchema = z.object({
  action: z.literal("skip_phase"),
  note: z.string().optional(),
});
export const AbortActionSchema = z.object({
  action: z.literal("abort"),
  note: z.string().optional(),
});
export const ReviseActionSchema = z.object({
  action: z.literal("revise"),
  instructions: z.string().optional(),
  note: z.string().optional(),
});
export const DiscussActionSchema = z.object({
  action: z.literal("discuss"),
  message: z.string().optional(),
  note: z.string().optional(),
});

/** Valid resume bodies for a prompt_review interrupt. */
export const PromptReviewDecisionSchema = z.discriminatedUnion("action", [
  ApproveActionSchema,
  ModifyActionSchema,
  SkipPhaseActionSchema,
  AbortActionSchema,
]);
export type PromptReviewDecision = z.infer<typeof PromptReviewDecisionSchema>;

/** Valid resume bodies for a phase_review interrupt. */
export const PhaseReviewDecisionSchema = z.discriminatedUnion("action", [
  ApproveActionSchema,
  ReviseActionSchema,
  DiscussActionSchema,
  AbortActionSchema,
]);
export type PhaseReviewDecision = z.infer<typeof PhaseReviewDecisionSchema>;

/** Any gate decision (use the per-gate unions when the gate kind is known). */
export const GateDecisionSchema = z.discriminatedUnion("action", [
  ApproveActionSchema,
  ModifyActionSchema,
  SkipPhaseActionSchema,
  AbortActionSchema,
  ReviseActionSchema,
  DiscussActionSchema,
]);
export type GateDecision = z.infer<typeof GateDecisionSchema>;
