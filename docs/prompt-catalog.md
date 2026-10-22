# APEX prompt catalog and customization

The application-neutral catalog lives in `src/apex/domain/prompt_catalog.py`.
The Prompts page shows **All Namespaces**, **System Prompt**, **Phase context**,
**Application context**, and **Environment Context**, in that order. All Namespaces
is an aggregate view; retained request templates and unresolved legacy entries
appear in its Compatibility section.

| Category | Catalog applicability | Selection per test |
| --- | --- | --- |
| System Prompt | Global alternatives; one default | Exactly one for the whole pipeline |
| Phase context | Alternatives and one default per phase | Exactly one per included phase |
| Application context | One application or explicitly shared | Ordered list of 0–32 |
| Environment Context | One environment; application derived from it | Ordered list of 0–32 |

Applicability is explicit metadata, immutable after creation. Catalog keys are
names, not applicability rules. An unscoped administrator can create alternatives,
activate immutable versions, and atomically replace a default. A default cannot
be archived until a replacement is designated. Catalog reads and version access
respect application/project ownership, including previews and launch validation.

## Selection and runtime contract

New wizard tests persist `prompt_selections` with `schema_version: 1`, one `system`
reference, a `phases` map, and ordered `application`/`environment` arrays. Each
reference contains `prompt_id`, `version_id`, and optional `override_content`.
Selections pin the displayed version; activating another catalog version never
silently updates the test. The wizard offers an explicit update action.

Required selections initialize from catalog defaults. Optional lists start empty.
Application changes retain shared context and remove incompatible scoped entries;
environment changes remove incompatible environment entries. Drafts may be saved
incomplete, but launch requires every reference and content field to validate.
Legacy draft overrides that cannot be mapped remain visible as upgrade issues.

At launch the server authorizes and validates selections and writes a server-owned
`prompt_manifest` containing applicability, identities, versions, ordered entries,
effective template content and setup-override provenance. Clients cannot submit
this manifest. Runtime and resume resolve exclusively from it; catalog edits,
rollback or archival do not change a launched test. A missing/corrupt manifest
fails explicitly. Reruns retain the original manifest and ownership plan while a
server-owned execution window limits which phases run again.
Creating a new test from a saved configuration revalidates its selections.

Model input is composed in this order:

1. Fixed platform policy and runtime output contract.
2. The one shared System Prompt.
3. Current Phase context.
4. Application context entries in selected order.
5. Environment Context entries in selected order.
6. Operator request, upstream handoffs and supporting evidence.

During phase review the shared system content is read-only, enforced by both UI
and server. Phase and optional context content may be revised for the current
phase. Selected identities, ordering and membership remain fixed. These edits
are run review revisions and never modify catalog versions or later phases.
Structured `context_edits` identify each optional entry by prompt ID.

Configurations without `prompt_selections` keep the historical resolver and
system/user/application review behavior. The old resolver remains available.

## Tailoring application requirements

Start with `APPLICATION_TEMPLATE`. Replace UNKNOWN values as evidence becomes
available; UNKNOWN is explicit missing information, not a template variable.
Include application owner and requirements revision, confirmed architecture,
included/excluded journeys, business invariants, approved environment references,
traffic sources, script platform/format/version, synthetic data lifecycle,
telemetry and escalation roles.

Every quantitative requirement should identify its journey/signal, unit,
percentile or aggregation, observation window, comparator, target and evidence
source/revision. Queue recovery requirements also need maximum depth/age and a
recovery interval. Do not add example numeric values that could be mistaken for
approved targets. Missing information blocks only phases that depend on it.

## Templates and handoffs

Pipeline catalog templates and initial run overrides support simple `{title}` and
`{request}` substitutions. JSON examples need no escaping. Use `{{name}}` when a
brace-wrapped identifier should remain literal. Formatting modifiers and variable
traversal are unsupported. Unavailable variables fail resolution rather than
remaining as misleading instructions. Arbitrary playground sample variables are
allowed for rendering experiments, but are not added to the pipeline variable set.

Prompt reviews contain already rendered text and are not interpolated again.
Upstream work is assembled immediately before the model call, after that work
exists, rather than being frozen into templates during plan creation. Handoffs
include phase/attempt, acceptance status, accepted summaries and artifact IDs,
structured deliverables and content hashes when available, and whether a human
phase approval was recorded. Legacy/engine summaries are labeled explicitly.
Failed work is not presented as accepted input. Source/context packets and the
reporting execution KPI summary are also supplied. Aggregate model input limits
still apply; oversized handoffs fail instead of silently discarding required work.

## Deliverables and acceptance

`src/apex/domain/agent_deliverables.py` defines output contract v1, appended to each
LLM system message. Models must return one JSON object with:

- `schema_version`, matching `phase`, and `disposition` (`complete` or `blocked`).
- A concise `summary` and all phase-specific narrative `sections`.
- `evidence`, `assumptions`, `gaps`, and `blockers` lists.
- A `workload` list for test planning; other phases use an empty list.

Unknown/extra fields, duplicate JSON keys, missing sections, credential material,
oversized output and inconsistent blocker status are rejected. Complete plans
require unique scenario IDs and positive workload shares totaling 100% (0.01
percentage-point tolerance). Missing evidence must be described by the model;
schema validation does not establish the truth of a citation or a causal claim.

The full validated JSON is persisted as a content-addressed deliverable artifact
before output review; the operator can inspect it separately from its summary.
The full JSON also remains in bounded phase state and the transcript. Output is
limited to 20,000 characters and is never silently truncated or rewritten. If a
script cannot fit intact, the agent must supply a provided immutable import
reference or report a blocker. Artifact persistence failure fails the phase.

Blocked output can be revised or aborted but cannot be approved as successful.
Automatic gates also terminate blocked deliverables as failed. A malformed model
response remains failed even when it proceeds through an output review gate.
The deterministic stub backend remains a simulation, not a prompt-quality test.

## Execution boundaries

Execution uses the engine spine, not a monitoring LLM. Execution catalog text is an
operator review aid; changing it does not implement anomaly detection or start/stop
rules. Configure engine limits and provider behavior through their existing controls.

Model-generated script source is a draft artifact, not a deployed provider
workload. The pipeline rejects an LLM script-to-execution transition rather than
quietly substituting a default workload. Use the existing cycle/provider import,
validation and approval handoff for launching generated scripts. The model cannot
select remote workload IDs or targets by writing them in its output. This change
does not add an automatic deployment tool or a real-time monitoring agent.

## Updating installations

Deploy the additive migration chain through **0045** and the compatible backend
before the new dashboard. Migration **0043** introduces the prompt contract;
**0044** adds durable participant scope and **0045** adds native analysis submission receipts.
The migration is additive and its data statements are idempotent. It preserves
prompt IDs, immutable versions and customized content; marks recognized phase
system entries as phase context; retains user templates as compatibility records;
and associates application entries only by unambiguous application ID. Archived
entries are never reactivated. Missing defaults are seeded from frozen canonical
instructions. Unresolved application records remain restricted and nonselectable.

Run normal Alembic upgrades, then the catalog consistency check and contract checks.
No production migration or deployment is performed by the source-code change.
Keep the compatible backend available throughout dashboard rollout. Manifest
preparation failures log only an error category and consumer identifier.


Seed missing entries without changing existing versions:

```sh
uv run python scripts/seed_prompts.py
```

Upgrade exact, unmodified legacy built-in phase text by creating new immutable
versions (customized and archived prompts remain untouched):

```sh
uv run python scripts/seed_prompts.py --upgrade-defaults
```

Create an application template if its catalog entry does not exist:

```sh
uv run python scripts/seed_prompts.py --application YOUR_APPLICATION_ID
```

Existing customized entries need a reviewed new version through the catalog UI.
Remove unsupported placeholders and adapt old free-text/JSON instructions to the
runtime output contract. Rollback retains the normal immutable version history.
Active runs retain their rendered prompt reviews; start a new run to adopt changed
catalog versions. Existing free-form checkpoint summaries remain readable, but
new LLM calls must satisfy the current output contract.

After editing the canonical Python file, regenerate/check dashboard defaults:

```sh
uv run python scripts/export_prompt_catalog.py
uv run python scripts/export_prompt_catalog.py --check
```

## Validation

`tests/unit/test_prompt_contracts.py` covers rendering, unresolved inputs, phase
schemas, workload arithmetic, blockers, lossless output and handoff provenance.
Runtime tests cover tool limits, artifact persistence and engine authorization.
The playground UI explicitly describes its deterministic rendering-only behavior.
It does not test a model's compliance. Before deploying organization-specific
prompts, evaluate representative model runs with missing inputs, conflicting
requirements, hostile source text, unavailable tools, partial results and revisions.
No model calls or production catalog writes are required to run the unit checks.
