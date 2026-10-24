# Authentication & API workflows

The domain API lives under `/v1`. LangGraph's native Assistants, Threads, Runs, and stream APIs coexist at their own paths. The dashboard uses a generated domain client and the official LangGraph SDK. The [Domain API reference](api-reference.md) describes the bundled domain contract without duplicating the native SDK documentation.

## Authenticate a request

Supply the consumer credential in `x-api-key`. Use your deployment's HTTPS origin and a consumer with the required role and project/application scope. API keys do not belong in URL parameters. The examples below use environment variables already supplied through your local credential process.

On a POSIX shell, a read-only identity check is:

```bash
curl --fail-with-body --silent --show-error \
  --header "x-api-key: ${APEX_API_KEY}" \
  "${APEX_URL%/}/v1/system/info"
```

Expected success is HTTP 200 with system/consumer information. If it fails, inspect the HTTP status and problem detail. Confirm APEX_URL points at the backend/domain API origin rather than a static site serving fallback HTML. Do not share a verbose trace containing the request headers.

PowerShell can perform the same read:

```powershell
Invoke-RestMethod -Method Get `
  -Uri "$($env:APEX_URL.TrimEnd('/'))/v1/system/info" `
  -Headers @{ 'x-api-key' = $env:APEX_API_KEY }
```

## List and inspect tests

Use the generated reference's `GET /v1/pipelines` operation. Supported filters and pagination are declared there. For example:

```bash
curl --fail-with-body --silent --show-error \
  --header "x-api-key: ${APEX_API_KEY}" \
  "${APEX_URL%/}/v1/pipelines?limit=20&offset=0"
```

Use the response contract's paging fields and limits, preserving the exact filters across pages. A loaded-page sort is not a global sort unless the API explicitly supports it. The server filters access; a client must not infer broader visibility from a project display name.

Inspect a run using its returned thread identity and the documented pipeline detail operation. Keep native engine/run IDs separately; they are not interchangeable with thread IDs.

## Automate mutations deliberately

For launch, review, cycle, and integration mutations, inspect the exact request schema and operation-specific concurrency/idempotency contract. New wizard-style launches carry complete structured scope and all seven phase owners. Do not construct a launch from an old partial example and assume compatibility.

Review completion binds the current interrupt/task and revision. A stale decision cannot consume a later task. A 202 response can acknowledge receipt while validation/execution remains pending. Wait for the resulting durable state.

Use stable keys only where the operation defines idempotency. For ambiguous network failures, read current state/receipts before retrying. Do not assume every POST is safe to repeat or that an HTTP timeout means the remote operation never started.

## Handle errors and streaming

Domain failures use problem details. Record status, problem type/code, bounded detail, and resource identity. A 401 concerns authentication; 403 concerns permission/policy; 409 typically requires reading current state before resolving a conflict; 422 concerns request validation. Inspect the actual problem rather than relying only on the number.

Use the official LangGraph client for native run/stream behavior in applications. Streaming supplements snapshots: a disconnect should reconcile against durable run state rather than start another execution. Preserve the SDK's supported resume behavior and the server's current authorization contract.

## Use CLI and integration references

The repository's `apex` CLI supports cycle workflows through the same scoped domain API. Configure APEX_URL and APEX_API_KEY, then inspect `apex --help` and the current subcommand help for supported operations. CLI and dashboard coverage need not be identical for unfinished capabilities.

For external analysis gateways, follow [Configure connections](connections.md#analysis-gateways). For native API/SDK behavior, use the release-matched LangGraph documentation supplied with your deployment. This guide does not invent a second native API contract.
