# Define test scope

Use **Scope** in [New test](/runs/new?step=scope) to identify every system whose context and evidence matter. You need operator access covering all selected participants, plus registered application/environment relationships. Viewers can inspect the recorded scope of authorized runs.

## Build the system topology

1. Add exactly one **Backend** participant. Optionally add one Frontend and any required Middle participants within the deployment's participant limit.
2. For each participant, choose a project, application, and environment. Application choices follow project access; environment choices follow the application.
3. Choose one participant as **Primary system under test**.
4. For live agent-owned execution, choose one included participant as **Traffic entry** and verify its approved target. The primary and traffic participant can be different.
5. Review the complete topology before continuing to Config.

The same application/environment pair cannot be included twice. Participant IDs and layer assignments belong to this test. The current maximum is 32 participants; an administrator can set a lower admission limit. Changing a project clears dependent application/environment choices. Removing a referenced participant clears its role assignment rather than silently selecting a replacement.

## Primary ownership and traffic entry

The **primary** participant determines project/application attribution for the run and its artifacts. **Traffic entry** determines the execution environment and the project used to resolve native load connections. Changing primary ownership does not redirect traffic.

For example, a test can belong to a backend application while traffic enters through a frontend application's approved environment. Middle systems supply dependency context. Merely including a backend or a middle system does not authorize the load engine to send traffic to it.

Simulation, analysis-only workflows, and human-owned execution can omit traffic entry. Context-only participants do not require load-target approval. Every included system still requires valid catalog relationships and your authorization.

## Handle scope changes

Changes can invalidate source selections, prompts, or project-bound setup. Confirm any displayed reconciliation notice and review affected Tickets, Knowledge, and Prompts steps. The wizard preserves authored workload/SLA values for revalidation; it does not establish that they remain appropriate for the new systems.

A copied historical run may have only scalar application/environment fields. Complete its layer, backend, and primary selections explicitly. The **Use as backend (primary system)** action offers the old selection as a starting point; it does not infer a historical topology.

## Verify launch scope

In Review, confirm every participant's layer, environment, primary role, and traffic role. Check the traffic destination independently from the run's owning project. Live targets are checked again by the server before execution.

The run retains a snapshot of permitted catalog context and scope roles. Credentials and environment options are excluded from that context. Access can be reevaluated after launch; losing access to a participant can make the run unavailable even if you previously opened it.

If a selector fails, use its Retry control before changing the topology. If an application is missing, verify its project registration and your scope with an administrator. For an unapproved or stale target, use [Applications & environments](catalogs.md#approve-a-live-target) rather than putting an alternative URL in a prompt.
