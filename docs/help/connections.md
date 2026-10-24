# Configure connections

Open [Connections](/admin/connections) with an admin session authorized for the intended audience. A connection binds an integration kind/provider to its endpoint, options, project ownership, and managed credential reference. A successful catalog save is not proof the provider is reachable.

## Create and verify a connection

1. Choose the appropriate kind and registered provider through the creation controls.
2. Enter a recognizable name and the project/application audience required by the provider policy. Shared/global connections require the appropriate administrative scope.
3. Complete the guided endpoint/options fields, or inspect the supported raw options when offered.
4. Set the managed secret reference required by that provider. Resolve it in the backend's deployment environment rather than putting a plaintext credential into test configuration.
5. Create the connection, inspect any immediate probe result, and open detail to test or correct it.
6. Verify the workflow with a small authorized integration operation before relying on it operationally.

Endpoint reachability is from the backend/provider runtime, not your workstation. A localhost URL on your browser machine may point somewhere entirely different from a container or cluster.

## Choose ownership and resolution

The server applies provider-specific scope policy. An explicit connection selection can take precedence over project/default resolution. For an existing run, inspect the connection it actually recorded; probing a different default does not diagnose that run.

Persisted execution/storage configuration needs the project/application ownership required by its workflow. For APEX Load, the remote project and application/release/workload binding are additional provider identities. Avoid assuming local and remote IDs are interchangeable.

Edits change connection generation and affect future resolution. An in-flight operation can retain its original connection affinity. Disabling a connection does not prove that remote work already submitted through it has stopped.

## Common integration kinds

- **Execution engine** supplies the APEX Load or LoadRunner operation path and configured workload binding.
- **Artifact storage** persists run evidence and supplies authorized retrieval. A report cannot rely on an artifact that was never durably stored.
- **Work tracking** supplies Jira Align requirements or supported Jira Cloud defect operations. Azure DevOps is retired for new provider calls.
- **Analysis** supplies LRA Engine, PerfReportForge, or Other Tools through the deployed service contract.
- Other registered kinds, including supported logs/source integrations, expose provider-specific guided options. Use the current registered-provider list and generated reference rather than inventing a provider name.

## Analysis gateways

External analysis services must expose the supported versioned capabilities, submission, status, and result contract. The adapter uses a stable Idempotency-Key for the same frozen evidence and expects a consistent job identity. Result validation binds tool and input digest and retains gaps/warnings.

LRA Engine and PerfReportForge are integration selections, not a claim that every existing installation exposes this contract. Provide a compatible service or gateway. Internal RCA instead uses APEX Load's native archive analysis API and its submission-recovery semantics.

Run a real acceptance exercise against each deployed gateway before declaring it ready. A capability probe is read-only and cannot validate full submission, polling, result collection, or artifact access.

## Diagnose probe failures

For authentication/secret errors, inspect the configured secret reference and backend deployment values. For DNS/connect/timeout errors, inspect the endpoint and network path from the backend. For a passing probe but failing run, inspect actual connection affinity, scope, workload eligibility, target approval, and required archive evidence.

Record connection ID, provider, generation, run/job identity, and bounded error detail. Follow [provider recovery](troubleshooting.md#provider-and-analysis-recovery) for uncertain submissions. Never replace a historical connection with the current default merely to force a stop or retry against an unverified endpoint.
