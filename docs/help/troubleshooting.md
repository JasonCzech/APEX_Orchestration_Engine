# Diagnose & recover

Start with the current run, operation identity, exact error, and selected scope. A failed request, an empty list, an unconfigured provider, and unavailable access need different actions. Preserve relevant work and inspect current state before repeating a mutation.

## Sign-in or access is refused

For a rejected key, confirm the intended backend and whether the key was rotated/disabled. In Settings, validate a replacement before switching. A viewer cannot launch or complete reviews. An operator also needs project/application scope; a role alone is insufficient.

For a missing multi-system run, verify access to every participant. Ask an administrator to distinguish missing registration, missing grant, and nonexistent data. Do not try alternative IDs to bypass authorization. [Roles & access](welcome.md#roles-and-access) describes the concepts.

## Launch is blocked

Open Review and follow each outstanding item to its field. Check the complete scope, exactly one backend, primary selection, and live traffic entry. Verify each participant's catalogs. Check engine, current approved target/version, provider mapping, storage, prompt selections, and compatible analysis connections.

Use Retry for failed catalog or readiness verification. A successful metadata check does not establish provider connectivity or workload eligibility. If a provider later refuses execution, retain the selected connection and workload identities.

For a copied draft, resolve explicit scope/prompt migration issues and ownership notices. Unsupported engines or incompatible analysis choices remain errors until you replace them. [Launch readiness](drafts-presets.md#launch-readiness) covers the final checks.

## A review is waiting or was superseded

A pending review is an expected pause. Inspect its type: prompt review, output review, human completion, or recovery. Discussion does not approve work. Human ownership requires a structured deliverable even under Run automatically.

If another operator acted, refresh the current review instead of replaying the old decision. If the same review's submission fails, correct the displayed issue or use the supported retry; the draft remains available. A changed task revision or evidence requirement can reset the old form.

Blocked deliverables cannot be approved as successful. Request a revision with concrete instructions or abort. A schema-drifted review exposes no actions because its contract is incomplete; align dashboard/backend versions and have an administrator investigate.

## Provider and analysis recovery

Identify whether the failure concerns provisioning, execution, collection, teardown, or report analysis. Use the pending recovery's Retry provider operation control for that operation. Preserve a known remote job/run identity. A remote success can still need local result collection.

For analysis, prior successful selections remain complete. Assign remaining work to a person only when you can supply the required deliverable and per-connection evidence. An uncertain native submission needs its receipt-based recovery; it must not become a silent duplicate.

Abort stops the orchestrated work it can confirm. If a remote engine continues generating traffic, have an authorized operator use the engine-specific stop path and verify provider state. Missing legacy connection affinity requires finding the original endpoint rather than substituting today's default connection.

## Connection or stream is lost

The static dashboard can remain visible while the backend is unavailable. Check the connection indicator and retry an appropriate read. If a stream reconnects, let the snapshot reconcile; losing transport does not imply the run stopped.

For recurring stream stalls, an administrator should inspect Redis/pub-sub and proxy buffering/read timeouts. For backend readiness failure, inspect database and runtime diagnostics. Do not launch a duplicate just to restore live updates.

## Results, sources, or lists look wrong

Check active filters, pagination, time window/timezone, selected project, and current access. Empty results differ from failed requests. Inspect full artifact identities and pending-versus-accepted state before comparing numbers.

Unknown metrics are not zero; an engine completion is not a pass. A source summary is not an original measurement. Preserve the source and run identities when evidence conflicts. [Results](results.md) explains these distinctions.

## Help or an API schema is missing

Use Retry article or Retry search after a chunk-loading failure. Refresh after a deployment if the current browser holds an older asset set. Unknown article/section links show a recovery notice; choose a current navigation topic. Compare dashboard documentation review date and connected system version when API behavior differs.

If the problem persists, prepare a [support request](support.md#collect-a-support-request) with reproducible steps and bounded evidence.
