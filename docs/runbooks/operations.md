# Operations runbook

Day-2 procedures for the APEX orchestration engine. All `/v1` calls use an
`x-api-key` header; required roles are noted per procedure (server-side
enforcement is canonical — ADR-0003). Error responses are RFC-9457 problem
details.

## Rotate a consumer key (role: admin)

Use when a key is leaked, an operator leaves, or on rotation schedule.

1. Find the consumer: `GET /v1/admin/consumers` (`listConsumers`).
2. Rotate: `POST /v1/admin/consumers/{consumer_id}/rotate` (`rotateConsumerKey`).
   The response contains the **new plaintext key exactly once** — only its
   sha256 hash is stored. Capture it immediately.
3. The old key is invalid the moment the call returns (identity is resolved by
   key hash on every request; there is no grace window). Update the client
   before rotating if downtime matters, or accept a brief 401 window.
4. Verify: old key returns 401, new key returns 200 on `GET /v1/system/info`.

To take a consumer out of service entirely, prefer
`PATCH /v1/admin/consumers/{consumer_id}` with `enabled: false`
(`updateConsumer`) over deletion — it preserves attribution history.

## Abort a stuck run (role: operator)

Two kill switches at different layers. Decide by asking: *is external load
still being generated?*

### Graph-level cancel — `POST /v1/pipelines/{thread_id}/abort` (`abortPipeline`)

First resort. Cancels the thread's pending/running LangGraph runs via the
loopback API. Use when the pipeline is stuck **before or between** engine
activity: spinning in an LLM phase, waiting at a gate nobody will answer, or a
run that should simply stop. Returns the cancelled run ids; `409
no_active_run` means there was nothing to cancel.

A gated (interrupted) thread is *not* stuck — resume it instead:
`POST /v1/pipelines/{thread_id}/gates/{interrupt_id}/resume` (`resumeGate`,
CAS semantics: a `409 gate_superseded` means re-fetch and present the current
gate).

### Engine-level kill switch — `POST /v1/engines/runs/{thread_id}/abort` (`abortEngineRun`)

Escalation. Use when the **external load run keeps burning** even though the
graph is gone (poll loop cancelled, server restarted mid-run, engine UI shows
load still ramping). It:

1. discovers the engine handle from thread state, falling back to the
   `engine_runs` projection;
2. tells the engine adapter to abort the external run — **failures propagate**
   (you must know if the kill did not land), then best-effort teardown;
3. cancels any remaining LangGraph runs on the thread;
4. best-effort marks the projection row `aborted`.

`404` means no engine handle is discoverable — nothing external was started.
If the adapter abort fails repeatedly, kill the run in the engine's own
console (LoadRunner/APEX Load) and record the thread id; the projection can
stay stale (checkpointed graph state is the source of truth).

## Prompt rollback (role: operator)

Prompts are versioned; the pipeline resolves the *active* version at run time,
so rollback affects new runs immediately (in-flight runs keep the version they
resolved, recorded per phase in `resolved_prompt_source`).

1. List versions: `GET /v1/prompts/{prompt_id}/versions` (`listPromptVersions`).
2. Sanity-check the target: `POST /v1/prompts/{prompt_id}/test` with
   `version_id` (`testPrompt`, 202 — runs on the `playground` assistant; fetch
   the result via the LangGraph API with the returned run/thread ids).
3. Roll back: `POST /v1/prompts/{prompt_id}/rollback` with
   `{"version_id": "<known-good>"}` (`rollbackPrompt`). `409` indicates a
   concurrent catalog change — re-fetch and retry.
4. Verify the active version on `GET /v1/prompts/{prompt_id}` (`getPrompt`).

## Connection probe triage (role: admin)

When a phase fails with adapter/connection errors:

1. Probe: `POST /v1/admin/connections/{connection_id}/test` (`testConnection`).
2. Triage by failure shape:
   - **auth/secret errors** — the row's `secret_ref` names an env var on the
     server (e.g. `env:APEX_MINIO_SECRET_KEY`); fix the deployment env (Helm
     `extraEnv` / compose environment), roll pods, re-probe.
   - **connect/DNS/timeout** — `base_url` is wrong for the network the *server*
     runs in (a classic: `localhost` instead of the in-cluster service name).
     `PATCH /v1/admin/connections/{connection_id}` (`updateConnection`).
   - **probe ok but runs still fail** — check scoping: explicit
     `connection_id` beats project-scoped beats global; confirm which row the
     run actually resolved (run config / phase result), not the one you probed.
3. To take a bad connection out of rotation: `POST .../disable`
   (`disableConnection`); resolution falls through to the next matching row.
4. Adapter instances are cached keyed by `(connection_id, updated_at)` — an
   admin edit invalidates the cache on the next resolve; no restart needed.

If Postgres is down, the resolver serves the static dev fallback (stub
adapters) and logs a warning — see `incident.md`.
