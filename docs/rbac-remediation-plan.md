# APEX RBAC — Code Review Findings & Remediation Plan

**Scope:** correctness + completeness review of the RBAC that another session implemented
against the enterprise blueprint (Phases 0–2 largely built; Phase 3 not started).
**Method:** 8 parallel adversarial subsystem audits (auth/key lifecycle, audit, scope
enforcement, LangGraph handlers, scoped-admin guardrails, settings/infra, dashboard, tests)
with independent per-finding verification. 33 findings confirmed, 6 down-scoped, 1 refuted.

## Headline

The implementation is **solid and genuinely enterprise-shaped**: peppered-HMAC key hashing
with lazy rehash-on-use, multi-key rotation with grace windows, soft-delete + deletion
records, scoped-admin guardrails, the closed LangGraph role holes, provider-query scope
injection, HSTS, and an append-only hash-chained audit log are all present and mostly
correct. **Correctness grade: B.** The blockers are concentrated in **one subsystem — the
audit log — which is not safe under concurrency** and has **attribution + coverage gaps**,
plus **one unverified tenant-isolation assumption** on the LangGraph KV store/crons. None of
these are visible in the happy path, which is why tests didn't catch them.

What is **not** a problem (verified, do not re-litigate):
- Scope enforcement is present on **every** scoped `/v1` route (dynamic `allows_scope`/`allows_project`/`_effective_project_filter`/`_constrain_jql/wiql`). The gap is the *guardrail/test*, not unprotected routes.
- Denied admin actions **are** recorded (the `/v1` 403 is caught by `AuthAuditMiddleware`) — the real defect is missing principal/resource attribution on those rows.
- Provider/log passthrough **injects** scope into JQL/WIQL/log filters now — the residual gap is only the absence of an independent test + adapter-level assertion.
- `listConsumers` already filters to the scoped-admin's manageable subset (`consumers.py:196-202`). The "shows all consumers" finding is **false**.

---

## Completeness by phase

| Phase | Status | Notes |
|---|---|---|
| **0 — Hardening** | mostly | Audit log, HSTS, DB-TLS hook, pepper, NetworkPolicy template, LangGraph role holes **closed**. Correctness defects in audit (below); TLS/HSTS/NetworkPolicy have config-drift/usability gaps. |
| **1 — Guardrails** | mostly | `allows_app`/`allows_scope`, scoped-admin guardrails, lifecycle audit columns **done**. `require_scope` is **dead code**; SCOPE matrix test classifies but doesn't **enforce** isolation. |
| **2 — Credential lifecycle** | done (w/ edges) | Multi-key, rotation grace, soft-delete, peppered hashing **done**. No brute-force lockout; legacy↔`consumer_keys` invariant not DB-enforced. |
| **3 — SSO/SCIM/tenancy/MFA/break-glass** | not started | Placeholder fields only in `routers/auth.py`. Dashboard still localStorage key. Large future effort. |
| **4 — Provider passthrough / compliance** | partial | Provider/log scope injection + global-connection restriction **done**. Audit verify/export/retention **missing**. |

---

## Remediation roadmap

### P0 — Correctness / isolation blockers (fix before relying on the audit log or shipping multi-tenant)

**P0-1 · Audit log is unsafe under concurrency (fork + silent drop).**
Two distinct bugs in `src/apex/services/audit.py`:
- `_previous_hash()` (`:109-111`) reads the chain head with no serialization → concurrent `append()`s read the same head and **fork** the chain (DAG, not a line) → tamper-evidence is void. *(verified: confirmed)*
- `_event_hash()` (`:160-172`) hashes only the `AuditEvent` fields + `previous_hash`, with **no timestamp/nonce**. Two identical concurrent denials produce an identical `event_hash` → the `UNIQUE(event_hash)` insert fails → `append_audit_event_best_effort` swallows it → **the event is silently dropped**.
**Fix (together):** serialize appends — wrap read-head + insert in one transaction with a Postgres advisory lock (`pg_advisory_xact_lock`) or `SELECT … FOR UPDATE` on a single `chain_tip` row — **and** add a server-set `event_at` (microsecond) + `nonce = secrets.token_hex(8)` field to `AuditEvent`, included in the hashed payload. Keep the unique constraint; it now only guards true duplicates.
**Files:** `src/apex/services/audit.py`, `src/apex/persistence/models.py` (AuditLog), new migration.
**Verify:** new async test fires N identical denials via `asyncio.gather`; assert N rows persisted, all `event_hash` distinct, and `previous_hash` forms a single linear chain.

**P0-2 · LangGraph store/crons/assistants tenant isolation is unverified and may be a no-op.**
`scope_filter()` returns a `{project_id: {$eq: …}}` **metadata** filter. It is correct for threads/runs, but `on_store_*`, `on_crons_*`, and `on_assistants_read` (`handlers.py:288-320`) return that same filter for resources that **may not be tagged with `project_id` metadata** — if they aren't, a scoped consumer can read/write another tenant's store/cron/assistant data despite the role gate. *(verified: confirmed risk; runtime behavior unverified)*
**Fix:** first **verify** against the installed LangGraph runtime whether the metadata filter is actually applied to store/cron/assistant ops and whether those items carry `project_id`. If not enforced, add explicit isolation — namespace-prefix store keys with the consumer's project (or validate the namespace tuple), and stamp/validate `project_id` on cron/assistant create — rather than relying on the filter.
**Files:** `src/apex/auth/handlers.py`, store/cron usage in `src/apex/graphs/**`.
**Verify:** integration test — a `project:p1` consumer cannot `store.get`/`crons.read`/`assistants.read` a `p2`-owned item (expect empty/403).

### P1 — Audit fidelity, coverage, and the scope guardrail

**P1-1 · Denials are logged without *who* or *what*.** `request_audit_event` (`audit.py:136-157`) builds events from the ASGI scope only, so every 401/403 row has `principal_id = NULL`, no role, no resource. A scoped admin probing escalation (`_ensure_can_grant` 403s) is unattributable.
**Fix:** resolve identity in `AuthAuditMiddleware` (or have the dependency stash it on `request.state`) and populate principal fields; in `consumers.py` emit an explicit `decision="denied"` security_event (with `resource_id`) before raising in `_ensure_can_grant`/self-guards.
**Files:** `src/apex/app/security.py`, `src/apex/routers/consumers.py`, `src/apex/services/audit.py`.

**P1-2 · LangGraph-surface denials are never audited.** `AuthAuditMiddleware` wraps only the `/v1` FastAPI app; 401/403 from the LangGraph handlers on `/threads,/runs,/assistants,/crons,/store` bypass it.
**Fix:** emit `append_audit_event_best_effort(...)` inside the LangGraph handlers' deny paths (`ensure_role`, scope checks, the `on_anything_else` fallback) before raising.
**Files:** `src/apex/auth/handlers.py`.

**P1-3 · Privileged mutations can commit with no audit row.** `_audit_consumer_action` is fire-and-forget (`asyncio.create_task`, never awaited), best-effort, separate transaction; and successful 2xx mutations aren't covered by the middleware. A dropped task = a key rotate/delete with zero trail.
**Fix:** `await` the audit write for create/rotate/delete/disable (still best-effort on read-side decisions), or write the audit row **in the same transaction** as the mutation; surface a warning/metric on audit failure.
**Files:** `src/apex/routers/consumers.py`, `src/apex/services/audit.py`.

**P1-4 · Make scope enforcement a guardrail, not a convention.** `require_scope` is dead code; the SCOPE classification test (`tests/unit/authz_matrix_data.py` + `test_authz_matrix.py`) checks that every route is *classified* but never proves an out-of-scope principal is *denied*. *(down-scoped from "enforcement incomplete" — today's routes do enforce; this prevents future regressions.)*
**Fix:** add `test_authz_scope_matrix` that, for each project/app-bearing operation, drives a scoped principal at an out-of-scope resource and asserts 403/404 — mirroring the role matrix's structural completeness so a new unscoped route fails CI. Either adopt `require_scope` on static-path routes or delete it and document the `allows_scope`-after-load pattern.
**Files:** `tests/unit/test_authz_matrix.py`, `tests/unit/authz_matrix_data.py`, `src/apex/app/dependencies.py`.

**P1-5 · DB TLS not actually enforced.** `db.py:28` sets `ssl` connect-args only for whitelisted `sslmode` values; an empty/absent `sslmode` enforces nothing (asyncpg may connect in plaintext), and the `db.py` check and `settings.validate_production_lockdown` duplicate logic that can drift.
**Fix:** in locked-down envs require an explicit TLS-bearing URI (fail boot otherwise) and default `ssl=True` for non-local Postgres; centralize the one check.
**Files:** `src/apex/persistence/db.py`, `src/apex/settings.py`.

**P1-6 · Negative-path test gaps.** No tests for: audit fork/collision under concurrency (P0-1), denied-authz attribution (P1-1), LangGraph handler denials (viewer can't create thread; scoped can't cross-project), store/cron isolation (P0-2), rotation grace (old key valid during grace, revoked after; grace=0), scoped-admin escalation attempts, and the scope isolation matrix (P1-4).
**Files:** `tests/unit/test_audit_service.py`, `tests/unit/test_auth_handlers.py`, `tests/unit/test_consumers_router.py`, new `tests/unit/test_authz_scope_matrix.py`.

### P2 — Hardening & robustness

- **P2-1 · Self role/scope change + PATCH TOCTOU.** Self-disable/self-delete are blocked, but a consumer can PATCH its own role/scopes; `_ensure_can_grant` prevents real escalation (so impact is low — escalation claim was overstated) but add an explicit self-role/scope guard and `SELECT … FOR UPDATE` around read-then-update. `consumers.py:236-269`.
- **P2-2 · Key-hash source-of-truth.** Legacy `ApiConsumer.key_hash` and `consumer_keys` can drift; resolution checks both (no bypass) but a valid key could stop working. Add an invariant/cleanup and plan to make `consumer_keys` authoritative. `repositories/consumers.py`, `service.py`.
- **P2-3 · Pepper lifecycle.** Lazy rehash-on-use already upgrades legacy→peppered when a key is used; add a proactive bulk rehash for never-used keys and assert pepper presence at runtime (not just boot). `service.py`, `settings.py`.
- **P2-4 · HSTS can be disabled** (`hsts_max_age_s=0`) with no lockdown check → require `>0` in locked-down envs. `settings.py`.
- **P2-5 · Rate-limit — remediated.** Locked/HA deployments now require the Redis
  backend. Request and run-create windows, failed-auth lockouts, and renewable SSE
  concurrency leases are atomic and shared across replicas; Redis failures fail
  closed. Local/test environments retain the bounded in-process implementation.
  `app/distributed_limits.py`, `app/security.py`, `settings.py`.
- **P2-6 · NetworkPolicy** is on-by-default with empty egress (breaks DB connectivity) → ship sane Postgres/Redis/DNS/K8s-API egress defaults. `deploy/helm/.../values.yaml`, `networkpolicy.yaml`.
- **P2-7 · Postgres firewall** allows `0.0.0.0/0` in dev with no prod guard → fail Terraform when `public_access` is set outside dev. `deploy/terraform/postgres.tf`.
- **P2-8 · `app_id` scope not enforced on the LangGraph surface** (`ensure_run_scope`/`scope_filter` use `project_id` only). Enforce app-narrowing or document app_id as informational. `handlers.py`.
- **P2-9 · Work-tracking adapter** is resolved with `project_ids[0]` only while the query constraint spans all scoped projects — reconcile (multi-project adapter or single-project requirement). `routers/work_tracking.py`.
- **P2-10 · Audit compliance tooling** — add chain-verify, export (JSON/CEF), and retention. `services/audit.py`, new `routers/admin/compliance`.

### P3 — Larger / future (Phase 3) & cleanup

- **P3-1 · Phase 3 (SSO/OIDC/SAML + SCIM + org/workspace tenancy + MFA + break-glass)** — unbuilt; tracked in the enterprise blueprint. Dashboard still stores the API key in `localStorage` with no session/idle timeout.
- **P3-2 · Remove or annotate placeholder fields** (`org_id`, `workspace_id`, `session_expires_at`, `mfa_required`, `step_up_required`) in `routers/auth.py` so the API contract isn't misleading until Phase 3 lands.
- **P3-3 · Studio identity = unscoped admin** in non-locked envs is correctly guarded; add a startup assertion that `is_locked_down` is true wherever `ENVIRONMENT` is prod/staging.

---

## Suggested sequencing
Land **P0-1** and **P0-2** first — the audit log can't be trusted and tenant isolation can't
be claimed until they're fixed. Then **P1** (audit fidelity/coverage + the scope isolation
test, which is the highest-leverage regression guardrail + TLS). **P2** is hardening polish;
**P3** is the Phase-3 program. Every P0/P1 item ships with the negative test that would have
caught it (P1-6).
