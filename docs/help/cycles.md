# Manage test cycles

Test cycles organize versioned engineering deliverables and execution preparation. The feature is deployment-dependent; navigation appears when enabled. You need access to the cycle's project/application and operator capability for mutations. Independent-review policy can require a different authenticated consumer for approval.

## Create and inspect a cycle

1. Open [Test cycles](/cycles) and choose **Create cycle** where available.
2. Select the project, application, and environment and enter a clear cycle name.
3. Open the cycle workspace. Overview summarizes deliverable progress, freshness, and execution information.
4. Work through Scope, Test Plan, Readiness, Script, Report, and Postmortem using the available navigation.

These six deliverables are different from the seven phases of a run. Progress through deliverables does not prove that an engine executed or that measured acceptance passed. Inspect execution and result state separately.

## Version deliverables and dependencies

Create a draft, complete the required engineering sections, and save it. Submit the exact version for review. Inspect its dependencies, revision, evidence, and source provenance before approval. Review confirmations identify the action and version.

An approved artifact is immutable. A later change creates a new version and can make dependent deliverables stale. Revisit stale dependencies rather than treating an old approval as authority for changed inputs. Archival/restoration changes lifecycle state and can also affect what is current.

Revision conflicts preserve edits for reconciliation. Reload the current version, compare changes, and deliberately resubmit. Repeating a stale revision is not a safe resolution.

## Capture sources and validate scripts

Where configured, use the Scope source controls to browse Confluence, preview readable page text, and capture an exact revision. Check source freshness before using it. Unsupported macros/attachments require an explicit disposition.

Script workflows can import supported APEX Load library material or approved sources, inspect immutable source identity, and request provider validation. Inspect validation findings and binding restrictions. A draft script, an imported source, a validated script, and an execution-approved workload are distinct states.

Model-generated source is a draft artifact. It does not deploy itself into a provider or select a workload through prompt text. Use the cycle/provider validation and approval path for generated scripts.

## Prepare and launch execution

Open **Executions**, select the approved inputs, and review preparation/preflight details. Resolve target, provider, source-equivalence, validation, and reservation blockers before launch. Confirm the exact operation and retain its durable receipt.

The supported live cycle path is bounded to the approved native APEX Load HTTP source contract. Other engines, source types, native scheduling, and publication can remain blocked or unavailable. Read the displayed eligibility and capability details; documentation is not a substitute for a current preflight.

Refresh reads recorded operation state and does not launch another execution. Recovery/cancellation uses the explicit current operation and confirmation. Retain uncertain dispatch identities and follow recovery rather than creating duplicates.

## Review reports and follow-up

Use Report to inspect execution summaries, frozen evidence, coverage, comparisons, and dependency freshness. A report references the evidence available when assembled. Summary-only evidence cannot establish raw-measurement verification or a causal diagnosis.

In Postmortem, record findings, gaps, and tracked actions with owners/status. Reuse supported prior-cycle material to seed a next-cycle draft, then revalidate sources, dependencies, target approval, and provider readiness. Reuse does not renew those approvals automatically.

External publication and defect workflows depend on their actual supported integration path. Approval of a report is not proof of publication. See [Capabilities & limitations](capabilities.md) before promising an end-to-end provider workflow.
