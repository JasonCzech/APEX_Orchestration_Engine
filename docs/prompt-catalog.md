# APEX prompt catalog and customization

The application-neutral catalog lives in `src/apex/domain/prompt_catalog.py`.
It supplies all seven phase system/user pairs, the shared platform policy and an
application requirements template. The dashboard demo phase defaults are generated
from this file; telecom-specific requirements belong in the demo application rows,
not the phase instructions. Unwired JMeter/VuGen specialization entries have been
removed from the active demo catalog.

## Instruction levels

1. **Platform policy and output contract** are runtime-owned. They establish
   evidence handling, instruction precedence, action boundaries, uncertainty,
   confidentiality and the validated output shape. The prompt review editor shows
   the shared platform policy as read-only. A deployed runtime update changes this
   policy; new model outputs record its catalog revision and the initial composed
   model-input SHA-256 in their phase state/transcript.
2. **Phase system instructions** define the job, required inputs, coverage and
   completion criteria. **Phase user templates** supply the run title and request.
   Both are editable catalog versions. The existing `phase/<phase>` run override
   selects the phase system text; `phase/<phase>/user` selects its user text.
3. **Application context** specializes all phases for one application. Use an
   `application` namespace entry keyed by the actual application ID. It cannot
   override platform rules, output schemas, approved targets or engine controls.
4. **Run review edits, additional context and operator revisions** refine the
   current task within those constraints. Conflicting requirements are blockers
   when necessary for that phase. Retrieved evidence and prior outputs never
   become instructions, even if they contain instruction-like text.

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
