# Work with APEX Load

APEX Orchestration coordinates engineering work and review. APEX Load owns its native workload, execution, scheduling, and detailed load-report capabilities. The [APEX Load workspace](/apex-load) exposes the configured integration and its recorded relationships. Each product enforces its own authentication and scope.

## Prepare an execution binding

An administrator configures an execution-engine connection with the intended remote project and provider settings. For an APEX Load test, Config includes an explicit application/release mapping. Resolve that mapping, the approved environment target, and eligible workload before launch.

Local project/application/environment IDs are Orchestration identities. Native project/application/release/run IDs belong to APEX Load. Similar names are not sufficient to bind the two. Check the connection and exact mapping shown in Review.

A native workload reference is execution authority only after the supported binding/approval flow validates it. Model-generated script text or an arbitrary native ID in a human note cannot establish that authority. Use [cycle script validation and preparation](cycles.md#capture-sources-and-validate-scripts) for supported generated-source workflows.

## Open the native console

When a run exposes an APEX Load execution link, open it to inspect that exact native run. Orchestration adds return context to supported console URLs. APEX Load can display an explicit return link for the matching execution, including the destination host.

The handoff does not copy API keys, sign-in sessions, or personal preferences between products. Sign in separately as needed. A return link provides navigation context; it is not proof that the integration is configured correctly or that the recipient is authorized.

## Inspect native results

Review native measurements and the returned outcome alongside orchestration progress. Engine completion, measured acceptance, and evidence availability remain separate. Verify that execution identity and source evidence belong to the requested run.

Orchestration validates returned outcome binding before persisting it. In human Execution with Internal RCA, the native run must also satisfy terminal state and remote project/application/release checks. Provider-derived measurements replace user-entered measurements when verified native results are retrieved.

## Mirrored runs and analysis

Where configured, native-run reconciliation can create or reference an Orchestration analysis pipeline. Use its stored pipeline link to investigate. Refresh refetches the ledger; it does not start another native execution or reporting pipeline.

A ledger status of launched means an analysis pipeline was created. Inspect its own progress and evidence to determine whether analysis succeeded. Missing native archives, stale assessment inputs, or unavailable RCA capability can still block [Reporting & Analysis](analysis.md).

## Troubleshoot the handoff

If the workspace is unconfigured, inspect the execution connection with an administrator. If a link fails, retain both local and native run identities and the destination host. Check each product's permissions independently.

Do not rewrite a console URL to point at another workload to bypass a binding error. Confirm the intended mapping and target, then repeat the supported verification/recovery operation. Use [Diagnose & recover](troubleshooting.md) for uncertain dispatch or collection.
