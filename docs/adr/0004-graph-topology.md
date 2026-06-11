# ADR-0004: Single master pipeline graph, config-driven phase routing

**Status:** accepted (2026-06-11)

## Decision
One `pipeline` graph contains all 7 phases (story_analysis, test_planning, env_triage,
script_scenario, execution, reporting, postmortem) as compiled subgraphs sharing the
parent state schema. A `plan` entry node resolves the requested phase subset from run
config (`configurable.phases` or `start_phase`/`stop_after`), validates preconditions
against accumulated thread state, emits `plan_resolved`, and a conditional-edge router
executes the plan. A thread = one pipeline run instance; partial/single-phase
executions are additional runs on the same thread.

## Alternatives rejected
- **Per-phase graphs as separate assistants:** fragments checkpointed state across
  thread/checkpoint namespaces, diverges stream shapes, multiplies assistant/config
  sprawl — and still needs a parent router for ranges.
- **Functional API:** no Studio topology view; channel/reducer state is awkward.

## Consequences
Phase selection is run config, never assistant identity ("golden configurations" are
assistants pinning config bundles). Re-running a phase overwrites its `phase_results`
entry; prior attempts remain recoverable via checkpoint history.
