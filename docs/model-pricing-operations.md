# Model pricing and retained usage

## Deployment

1. Keep `APEX_MODEL_PRICING_ADMIN_ENABLED=false` during rollout.
2. Run `alembic upgrade head` using the deployment's migration procedure. Revision
   0042 adds model pricing, correction jobs, admission records and usage attribution.
   It preserves original costs and widens cost capacity while keeping six decimals.
3. Roll every FastAPI and native LangGraph worker to this version. Older writers
   cannot enforce archive decisions or capture consumer ownership.
4. Verify schema readiness and the `model-pricing-reconciler` heartbeat.
5. Enable `APEX_MODEL_PRICING_ADMIN_ENABLED=true` on API instances after rollout.
   Set `APEX_ANALYTICS_COST_VISIBLE=true` to expose estimated costs to authorized
   administrators and users viewing their own history.

The dashboard entry is **Agent analytics → Manage model pricing**. The API returns
503 with a deployment explanation while administration is disabled. This switch
controls administration only; attribution, retention and archive admission checks
remain enforced.

## Data and recovery

Prices are USD per million tokens. Current/future prices create a complete,
immutable timeline generation. Historical changes require a date range, reason,
preview and explicit confirmation. Original event costs never change; published
adjustments determine corrected costs. Missing prices remain null, not zero.
Confirmation retains the exact totals reviewed in the preview; final job totals
also include usage that arrived afterward. Preview tokens are signed using the
shared authentication pepper. Local development without a configured pepper
requires a fresh preview after a process restart.

The worker processes 500 events per transaction using a database lease. A worker
restart resumes after lease expiry. Repeated confirmation uses the same idempotency
key. Failed corrections retain staged rows; retry them from the model panel. The
active job is rediscovered after a page reload. Publication and its administrative
receipt are transactional. Inspect `pricing.worker_failed`,
`pricing.job_failure_record_failed`, and `agent_events.pricing_failed` logs when
investigating missing pricing or stalled corrections. Logs omit database exception
messages and connection credentials.

Configured model reconciliation never clears archive state or replaces an existing
pricing generation. Historical registry backfill is bounded and resumable, and does
not infer ownership from display names. Unattributable historical usage stays visible
only to administrators within their authorized scope. Backfilled records covered by
published corrections receive adjustments without changing original costs.
Model work retains the invoking consumer and occurrence time across review gates.
Each output revision has its own usage snapshot and replay key, so another user's
revision does not overwrite earlier usage or inherit its latency.

Archiving blocks new admissions; stored admissions allow an existing execution to
resume with unchanged model choices. A new run/rerun must pass the current allowlist
and archive checks. Change the configured default before archiving it. Restoration
does not modify the allowlist. No operation in this feature deletes history.

## Verification

Run `pytest tests/unit/test_model_pricing_history.py` for SQLite-backed invariants.
Set `APEX_PRICING_TEST_URI` to a **test-only PostgreSQL database** to additionally run
the same cases in disposable schemas, removed after each test. The suite covers
correction publication, lease recovery, late arrivals, original cost retention,
zero-price boundaries, archiving/admission, and HTTP privacy/export behavior.

Browser workflows are in `apps/dashboard/e2e/model-pricing.spec.ts`. They exercise
editing, immutable history, correction confirmation, archiving, retained usage,
keyboard focus and narrow-screen layout using deterministic API fixtures.
