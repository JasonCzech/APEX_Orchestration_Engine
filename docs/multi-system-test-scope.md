# Multi-system test scope implementation specification

## Contract and ownership

Every new pipeline launch supplies `TestScopeV1` in `test_scope`. Each participant
has a stable row ID and a project/application/environment triple. Layer membership
is specific to the test: zero or one frontend, zero or more middle participants,
and exactly one backend. Duplicate application/environment pairs and participant
IDs are invalid. One included participant is primary. An independently selected
traffic entry is mandatory when an agent performs live execution.

The primary participant owns the run's project/application attribution. The traffic
entry owns its execution environment and the project used to resolve native load
connections. Changing primary does not redirect traffic. Simulation, analysis-only,
and human-owned execution may omit traffic entry. Engine/provider choice remains
explicit. The deployment admission limit defaults to 32 and cannot exceed 32;
lowering it does not invalidate previously recorded scopes.

Legacy scalar fields are compatibility projections, not a second topology. New
requests without structured scope receive an actionable validation error. Historical
runs retain historical rules; copying one requires explicit layer/backend/primary
completion. No legacy application is inferred to have been a backend.

## Persistence and security

The server validates every catalog relationship and every participant grant before
launch. It resolves an allow-listed snapshot of application descriptions,
environment names/kinds, and host inventory; environment options and credentials
are excluded. Live destinations additionally carry approved URL/version data,
which execution rechecks before dispatch. Context-only participants do not need
load-target approval.

Migration 0044 adds normalized run-participant membership and a canonical binding
header. The binding records scope, participant count, and digest. Projection writes
are atomic; incomplete or inconsistent projections fail closed. The same verified
membership must govern native and REST reads, operations, artifacts, reports,
streams, and run-linked aggregates. Owner-based counts count once; membership
filters do not join a run into multiple rows. Access is reevaluated after launch,
including revocation of one participant grant.

## Setup and reconciliation

Scope rows are grouped by layer with dependent project/application/environment
selectors. Project changes clear application and environment; application changes
clear environment. Removing a referenced participant clears its primary/traffic
reference instead of choosing a replacement. The editor exposes primary and traffic
roles separately and derives the owning project.

Saved drafts, copy setup, review, launch previews, and launch intent fingerprints
carry the complete topology. Scope-neutral presets preserve it. Structured presets
with null traffic clear the old execution environment. Scope-changing legacy
presets require completion. Ownership changes clear project-bound ticket/preset
associations but preserve authored workload/SLA configuration for revalidation.
Participant catalog queries validate all selected rows and provide explicit retry.

Knowledge and prompts are selected across participating applications/projects,
with source identifiers and authorization intact. Prompt applicability uses all
participant application/environment IDs and retains pinned versions and review
semantics. Work tracking retains its single-connection restriction.

## Runtime and reporting

The frozen snapshot flows through configurable state, phase composition,
checkpoints, human handoffs, analysis, and reports. Context distinguishes primary
objectives from traffic destination, backend evidence, and dependency context.
Prompt context is labeled with participant/layer/role and shared application
context is deduplicated. Provider resolution for native execution and internal RCA
uses traffic ownership while run/artifact attribution retains primary ownership.

Findings may reference included participant IDs. Evidence without a reliable
participant association remains run-level/unattributed. Run detail and review show
all layers, environments, primary, and traffic roles. Historical reporting states
that detailed topology was not recorded.

## Verification and acceptance

Focused contract tests cover layer counts, duplicate pairs/IDs, catalog ownership,
invalid references, limits, safe context, projection integrity, and revocation.
REST launch regressions exercise four participants across four projects, primary
in each allowed layer, frontend traffic independent of primary, and context-only
participants without target approval. Partial access is rejected before catalog or
thread I/O. The graph regression carries a four-participant topology through all
seven phases and checkpoint state.

Release acceptance additionally requires dashboard save/reload/copy and launch
retry tests, native and REST authorization surface tests, migration/model-drift
checks, target-version dispatch safeguards, and combined repository validation.
The manual browser check uses the local demo; it does not substitute for database,
provider, or cross-project security tests.
