# Capabilities & limitations

This guide documents the current Orchestration dashboard and its implemented contracts. Actual availability depends on the deployed backend, feature configuration, permissions, integration adapters, approved targets, and remote services.

## Simulation and connected execution

The Simulated engine does not generate live target load. Connected agent phases can still use models and source providers. The offline demo instead runs entirely against bundled data and makes no integration/model calls. Demo targets, prices, timing, and results are illustrative.

An engine/provider listed in a catalog is not proof of live operational readiness. Metadata checks, a passing capability probe, a validated workload, and a successful end-to-end exercise establish different levels of evidence.

## Workload and script boundaries

Orchestration authors bounded requested VUsers, duration, linear ramp, and supported acceptance thresholds. It does not expose the provider's complete pacing, think-time, custom-ramp, or hard-stop configuration.

Model-generated script source is a draft artifact, not a deployed workload. A prompt cannot choose an arbitrary remote workload ID or redirect traffic. Use the implemented provider import/validation/approval flow. Native APEX Load cycle execution is restricted to its supported approved HTTP source contract; unsupported source-equivalence paths remain blocked.

## Human phases and analysis

All seven phases in new wizard tests have explicit owners. Human assignment means required human completion. Automatic review does not remove human work. Legacy runs keep historical phase semantics.

External analysis needs compatible deployed services/gateways and authorized artifact access. Provider names do not guarantee such a gateway exists. Internal RCA needs a valid native archived execution and current assessment. Local abort is not a guarantee of remote analysis cancellation.

## Sources and lifecycle

Jira Align supports the current requirements workflow and Jira Cloud supports applicable defect workflows. Azure DevOps is retired for new calls. Work-tracking selections retain a single-connection boundary.

Cycles are feature-gated. Approved deliverables, live preflight, actual dispatch, measured acceptance, baseline promotion, and external publication are separate authorities. Supported Confluence scope capture does not imply complete macro/attachment capture or publication support. Use explicit current readiness and capability feedback.

## Evidence and conclusions

Schema validation establishes the shape of a deliverable. It does not independently verify a model's statements, citations, or causal findings. Unknown and incomplete measurements remain unknown. Summary-only coverage does not establish full raw-evidence verification.

Results distinguish lifecycle completion, acceptance assessment, and evidence availability. No missing measurement should become an implicit pass or zero. Compare like-for-like scope, workload, and coverage before interpreting differences.

## Version and operational boundaries

Documentation and domain OpenAPI are bundled with the dashboard. The backend may be deployed separately, and locked-down environments can disable live Swagger. Native LangGraph APIs follow their own release-matched SDK contract.

For an unsupported workflow, retain the exact capability/error and use your organization's administrator or service desk. [Support](support.md) lists the information needed for diagnosis without exposing credentials.
