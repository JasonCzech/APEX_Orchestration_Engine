/**
 * @apex/pipeline-events — zod contracts for the APEX pipeline's streamed
 * payloads (schema_version 1): custom SSE events, HITL interrupt payloads,
 * gate resume bodies, and lenient thread-state mirrors.
 *
 * Boundary policy:
 * - events.ts / interrupts.ts GATE: strict known fields + `.passthrough()` for
 *   unknown fields; unknown event `type` / gate `kind` is rejected by the
 *   discriminated unions — route to a SchemaDriftReporter via
 *   parsePipelineEvent / parseGateInterrupt (fail-loud dev, tolerate-log prod).
 * - state.ts MIRRORS: everything passthrough and optional-leaning; it must
 *   parse partial mid-run state, never block rendering.
 *
 * Follow-up (tracked for integration): tests/fixtures.test.ts hand-lifts its
 * fixtures from the backend's own asserted payload literals
 * (tests/unit/test_pipeline_graph.py, tests/unit/test_execution_phase.py).
 * Replace with a generated-fixtures pipeline (backend test emits JSON, this
 * package parses it) once one exists.
 */
export * from "./events";
export * from "./interrupts";
export * from "./state";
