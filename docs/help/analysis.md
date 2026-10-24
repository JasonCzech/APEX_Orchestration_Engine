# Reporting & analysis tools

Reporting & Analysis can run compatible integrations before report assembly. Configure them in the wizard's **Config** step. The selected connection identities are recorded with the test and checked against engine compatibility, project access, and deployment configuration.

## Select compatible tools

- **LRA Engine** is available for LoadRunner workflows through a compatible analysis service/gateway.
- **Internal RCA** uses APEX Load's native archived-run analysis and requires a verified native run identity.
- **PerfReportForge** can support either real engine through a compatible service/gateway.
- **Other Tools** can select multiple distinct analysis connections for either real engine.

Each named tool can occur once, and a connection cannot be selected twice. The workflow supports up to 16 selections. The default analysis-attempt deadline is 1,800 seconds; a configured deadline must be within the supported limit. Simulated execution has no external analysis integrations.

Changing engines preserves incompatible selections as visible errors until you replace or remove them. Select connectors by name and inspect their provider rather than assuming two similarly named connections are interchangeable.

## Understand automatic reporting

For agent-owned reporting, integrations run in saved order: engine-native analysis, PerfReportForge, then Other Tools. Each successful result is validated and stored as a run artifact before the report can finalize.

The report consumes complete validated evidence, including findings, warnings, gaps, and provenance. A short status preview is not the full result. If the complete required evidence exceeds the input budget or cannot be retrieved intact, report assembly enters recovery rather than silently omitting it.

Connection probes read capabilities; they do not submit a real analysis or certify its output. Existing LRA Engine or PerfReportForge installations may need an adapter/gateway implementing the Orchestration contract. Selecting a provider label does not create that service.

## Inspect job progress

The reporting ledger records tool/connection, attempt, frozen input identity, remote job identity, status, and result artifact references. A remote success can still be collecting until its validated result is durably stored. Polling backs off while waiting; transient errors can retry within the deadline.

For Internal RCA, native archive finalization, current-assessment availability, and evidence freshness are prerequisites. An old archived analysis is not substituted as proof that the newly selected analysis completed.

## Recover analysis

When reporting opens a recovery review, inspect the failed tool, job identity, attempt, and error. Use **Retry provider operation** to continue through the supported recovery path. A known job is retained for polling/collection; earlier successful tools remain succeeded.

An explicit retry after a terminal remote failure starts a new attempt/key. An uncertain submission is handled using its recorded submission identity and recovery semantics; do not work around it by creating another test blindly.

**Assign remaining work to a person** records a runtime ownership override and creates the required human task. Collected successful artifacts remain available. Supply complete reporting output and references for each selected connection, including the remaining human work.

Abort stops local orchestration/polling. Remote cancellation is not guaranteed; retain the job identity for provider-side follow-up. A success receipt for an accepted recovery request is not proof that report assembly has finished.

## Complete reporting as a person

A human-owned phase does not automatically call these tools. Its completion requires the reporting deliverable and a nonempty result-reference list for each selected connection. References must use permitted run artifacts or safe external report URLs included in evidence. Missing, extra, or duplicate connection references are rejected.

See [Complete human work](phases.md#complete-human-work) and [Configure connections](connections.md#analysis-gateways) for the corresponding task and service requirements.
