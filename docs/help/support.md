# Support, glossary & documentation

Contact your organization's APEX Orchestration administrator or service desk. This self-hosted app does not configure a universal vendor support address. Ask the deployment owner for the correct internal support channel.

## Collect a support request

Include the following information, using only evidence you are permitted to share:

```text
Dashboard documentation review date:
Connected system version/environment:
Time of problem and timezone:
Screen/article URL (remove sensitive query values):
Role and relevant project/application scope (no API key):
Run/thread, phase, interrupt/task, or cycle/version identity:
Connection/provider and native job/run identity, if applicable:
Steps to reproduce:
Expected result:
Observed result and bounded error/problem detail:
Whether the operation may already have succeeded remotely:
Relevant artifact IDs and permitted screenshots:
```

Preserve full identities rather than truncated visual labels. Include filters and timestamps for analytics/log issues. Do not include request authentication headers, plaintext keys, secret values, raw sensitive source documents, or unrestricted artifact dumps.

For active unwanted load, follow your operational incident path immediately and have an authorized operator verify engine stop. Filing a support request does not stop an external run.

## Glossary

- **Test**: configured performance-testing work and its objective.
- **Run**: a recorded attempt with progress and evidence. A thread is its underlying workflow identity in native APIs.
- **Phase**: one of the seven engineering/execution stages.
- **Review / gate**: a pending workflow interaction bound to an interrupt.
- **Decision**: the recorded authenticated action on a review.
- **HITL**: human in the loop; phase ownership can require a person's structured completion.
- **Primary system**: scope participant owning project/application attribution.
- **Traffic entry**: included participant whose environment is the live execution destination.
- **Deliverable**: structured engineering output with sections, evidence, and disposition.
- **Acceptance**: an assessment of requirements/thresholds, distinct from lifecycle completion.
- **Artifact**: persisted evidence identified by its run/phase and stable reference.
- **Manifest**: the frozen authorized prompt selection/content recorded for a launch.
- **Cycle**: versioned engineering deliverables and associated execution preparation.
- **Idempotency key**: operation identity used by a specific supported contract to recover an unchanged request without duplicating its effect.
- **Connection affinity**: the recorded provider connection associated with an operation.

## Documentation scope

The operator, administrator, and API guides were reviewed against repository workflows on **2026-10-24**. They are maintained by **APEX Orchestration maintainers** and shipped with the dashboard. The Help footer gives the review date and, when authenticated, the connected system version. Settings and System status provide additional runtime information.

This baseline covers multi-system scope, seven explicit phase owners, prompt-manifest selection, human completion, analysis recovery, and the supported cycle execution path. Historical runs and older drafts can expose compatibility behavior described in the relevant articles.

The bundled generated API reference describes the committed domain contract. It does not establish the version of a separately deployed server or replace its permission checks. Documentation is not live provider acceptance evidence.

## Maintain or report an article

Report an inaccurate article using Copy article link, the documentation review date, and a concrete before/after example from the app. State which deployed version you observed. Treat missing instructions and broken contextual links as documentation defects.

Maintainers edit canonical Markdown under `docs/help/`, the typed catalog, and relevant contextual mappings in the same change as behavior. The documentation integrity checks validate catalog membership, headings, links, search coverage, and screen mappings. Walkthrough screenshots use reproducible demo data so they can be refreshed without exposing connected customer information.

Use [Capabilities & limitations](capabilities.md) when an article and a deployment capability appear to conflict. Confirm the current contract and provider configuration before treating an unavailable path as a documentation-only issue.
