# Deployment & operations

This guide is for platform administrators. [System status](/admin/system) and [Settings](/settings) expose runtime information; infrastructure changes are performed through the deployment's controlled procedures. The dashboard is a static SPA deployed separately from the LangGraph/domain backend.

## Configure the dashboard

The dashboard reads `/config.json` before mounting. `apexOrigin` selects the domain API origin and `langgraphOrigin` selects the native LangGraph origin. Empty origins use same-origin routing. Origins must be complete HTTP(S) origins without credentials, paths, queries, or fragments.

`dataMode` is connected by default. Demo uses the bundled offline dataset, requires empty API origins, and does not contact models, engines, or source providers. It cannot be activated by a URL flag. Deployments generate the appropriate content-security policy for allowed API origins.

Static hosting needs SPA fallback so article/run deep links survive reload. Reverse-proxy the domain and native API paths to the correct services. Event streams need disabled buffering and long read timeouts. Cross-origin deployments must configure both API/CORS policy and frontend CSP explicitly.

## Inspect health and readiness

Use system information and dependency diagnostics with an authorized session. `/ok` is the runtime liveness surface; `/ready` is an opaque dependency-aware readiness probe. A responsive static dashboard does not establish backend or provider readiness.

Check the packaged/database migration compatibility gate before rolling out backend replicas. Use the repository's deployment manifests, immutable release images, configured migration job, and current runtime/license requirements. Do not copy commands from an old release without checking its contract.

The backend relies on PostgreSQL for durable graph checkpoints and domain data. Redis carries cross-replica stream pub/sub. Artifact storage holds referenced evidence. Provider credentials and external endpoints remain independent dependencies.

## Back up and restore

Back up domain data, LangGraph runtime/checkpoint data, artifact storage, and the required deployment secrets using your organization's recovery controls. Database-only backups cannot restore evidence if the artifact objects are missing. Protect the key-hashing configuration alongside the consumer records it authenticates.

Test a consistent restore into an isolated environment. Verify catalog/authentication, run checkpoints, artifact retrieval, and provider affinity before allowing resumed external operations. Preserve operation receipts so recovery can distinguish an existing remote job from a new submission.

## Recover dependency incidents

For PostgreSQL failures, durable progress or domain requests can fail. Restore database availability and reconcile the exact current run/checkpoint before resuming. Do not assume lost usage/projection records reconstruct themselves completely.

For Redis/stream failures, the workflow may continue while the live transport stalls. Restore the dependency and let the client reconnect/refetch durable state. A stream disconnect alone is not evidence that load generation stopped.

For artifact failures, preserve the original job identity and use collection recovery. For a remote engine still generating load after graph cancellation, use the engine-specific stop path and inspect confirmation. Retain legacy connection-affinity errors for manual reconciliation with the original provider.

## Rollout and documentation version

Ship documentation and the dashboard together. The guide's review date identifies its content baseline; Settings/System status reports the connected system version. The bundled OpenAPI contract can differ from a separately deployed backend, so compare versions when diagnosing schema drift.

The repository maintains detailed deployment/operations/incident runbooks under `docs/runbooks/`, Helm under `deploy/helm/`, and infrastructure under `deploy/`. Deployment owners should use those release-matched sources for infrastructure commands. [Support](support.md#documentation-scope) describes the guide's review and maintenance boundary.
