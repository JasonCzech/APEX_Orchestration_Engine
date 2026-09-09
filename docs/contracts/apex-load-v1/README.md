# APEX cross-application contracts, version 1

APEX Load produces `outcome` on `GET /api/v1/tests/{id}/archive/report`. Orchestration validates it, binds `execution_id` to the requested run, and persists it in the execution summary. Native analysis imports preserve the same object. See the JSON schema and representative fixture in this directory.

## Assessment

`passed`, `failed`, `inconclusive`, `not_assessed`, and `unavailable` describe computed acceptance, separately from execution termination and evidence coverage. Completion never implies acceptance. Analyst annotations do not override this object. `observed_at` is the time Load computed the report, not the test end time. Durations are seconds. Missing durations, coverage, and criteria revisions remain null; they must not be guessed. Scope IDs come from archived provenance.

Old Load versions can omit `outcome`; the existing nullable `passed` contract remains supported. Version 1 is authoritative over the legacy boolean. Unsupported or malformed provider outcomes fail collection; malformed stored outcomes suppress a legacy pass in public projection. Coordinate schema changes across both consumers before changing the producer's version.

## Evidence links

Supported report destinations are `#/report/{executionId}?section=summary|performance|diagnostics|evidence`. Load synchronizes these with its section tabs and browser history. Existing test/report routes continue to work. Orchestration only constructs these from validated HTTP(S) execution URLs; credentials, unrelated parameters, and invalid execution identifiers are rejected.

Return context stays in the outer URL query: `orchestrationReturn` and `orchestrationRun`. The existing return validator requires an exact execution match. Report pages expose the same return action as execution pages.

## Release mappings

Configure an APEX Load connection with an existing `project_id` and:

```json
{"release_mapping":{"application_id":"load-app-id","revision":"mapping-v1"}}
```

Use one connection for each mapped Load application. The native capabilities response includes the mapping and active release choices, validated against the exact provider application/project. The run wizard exposes this catalog when an enabled native connection is selected. The chosen `load_test.release_id` and `load_test.mapping_revision` survive review and are checkpointed before provisioning.

Provisioning checks the selected revision, exact application/project/release ownership, and archived status before uploading scripts or creating a test. It writes application/release IDs into the provider test request and stores all three IDs plus the mapping revision in the execution handle. Retry adoption checks that the existing test matches the selected application/release. A changed mapping requires an explicit review and a new run identity; historical handles are never recomputed from current settings. Run-specific options cannot override the connection mapping.

Native imports use the archived run's scope rather than assigning the current connection mapping to historical executions. An imported run has no Orchestration launch mapping revision unless one was recorded at launch; the UI says “Not recorded.”

## Rollout and validation

Deploy Load's additive report contract first, then Orchestration's consumer. Existing connections without mappings continue using the old launch behavior. Add a mapping and increment its revision whenever its meaning changes. Validate with an enabled, project-scoped connection before production use; local fixtures do not prove a deployed end-to-end handoff.

See [validation results and remaining checks](VALIDATION.md).
