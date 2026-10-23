# Native run-create controls vs `/v1` facade launches

The `/v1` facade creates runs with `loopback_client()`. That is langgraph_sdk `get_client()` with no URL, and its in-process ASGI transport uses `root_path="/noauth"`. langgraph_api's `ConditionalAuthenticationMiddleware` skips authentication for that root path, so no custom auth handler in `src/apex/auth/handlers.py` runs for facade calls. The table checks each control in `on_threads_create_run` against what the facade does itself.

**Scope of the facade:** the wizard launch (`create_pipeline_run`), legacy launch recovery, Cycle dispatch, the APEX Load reconciler (`start_run`), phase reruns (`rerun_pipeline`) and gate resumes.

| Native hook control (`handlers.py`) | What it protects | Facade equivalent | Status |
|---|---|---|---|
| `ensure_role(OPERATOR)` | Who may start runs | `require_role(Role.OPERATOR)` on the launch, rerun and resume routes. Cycle dispatch and the APEX Load reconciler are server-owned callers (the reconciler is a background principal), which the native hook also treats as trusted. | Covered for API callers |
| `_validate_run_persisted_inputs` | Credential-shaped or oversized persisted JSON | Pydantic request models, `validate_json_object`, and `contains_credential_material` in `start_run` | Covered |
| `_reject_untrusted_launch_metadata`, `created_by` stamp | Caller-forged launch metadata | The facade builds thread and run metadata itself; it does not stamp `created_by` (nothing reads it today) | Covered (no stamp) |
| `ensure_run_controls`: webhooks, `after_seconds`, `interrupt_*`, `multitask_strategy`, durability | Direct SDK misuse | The facade passes fixed values (`multitask_strategy="reject"`, `durability="sync"`, no webhook or interrupts) | Not applicable |
| `ensure_run_controls`: `rerun_phases`, `cycle_dispatch_id`, `prompt_manifest` server-owned | Forged server-owned fields | Request validator and `start_run` refuse them (`apex_actor_id` added on this branch) | Covered |
| `ensure_run_controls`: provider workload selectors | Per-run `script_refs`/`test_id` overrides | Route rejects them; reruns and copies use the durable replay barrier | Covered |
| `ensure_run_controls`: model allowlist (via `PipelineConfigurable`) | Non-allowlisted models | `PipelineConfigurable.model_validate` in `start_run` runs `validate_model_by_phase` | Covered |
| `_materialize_native_launch_defaults` | Assistant defaults pinned before checks | `assistant_launch_defaults` plus the snapshot freeze in `start_run` | Covered |
| `ensure_run_scope`, `_ensure_catalog_app_scope` | Project/application authorization | `_resolve_pipeline_scope` and `resolve_test_scope` against the catalog | Covered |
| `test_scope` required + `verify_persisted_test_scope` | Unscoped launches, scope drift | The wizard route requires `test_scope`, and `persist_test_scope` binds it before the run. Cycle dispatch and the reconciler launch without one, matching the native exemption for trusted loopback. | Covered for API launches |
| `ensure_run_environment` | Approved execution target stamped server-side | `resolve_test_scope` snapshot sets `environment_target`; caller targets are refused | Covered |
| Snapshot freeze of effective config | Assistant edits racing a launch | `validated_config.snapshot()` in `start_run` | Covered |
| `require_analysis_readiness` | Incompatible analysis selections | `check_execution_before_start` on the wizard route. Reruns now recheck it before admission (they had replayed without it). | Fixed on this branch for reruns |
| **`admit_models`** | Archived-model refusal, defaults pinned at admission, `model_run_admissions` record | **Was missing** | **Fixed on this branch:** `start_run` and `rerun_pipeline` admit before `runs.create`. Archived models return 409 (as natively); other refusals return 422 with the reason; an unavailable catalog returns 503; a fresh thread is removed on refusal; an adopted replay is not admitted again. Sparse launches (no `test_scope`) admit with the assistant's model choices, as the native merge would. A launch retried on its already-bound thread (Cycle dispatch) admits with `resume=True`, so it keeps its first admission instead of being stranded. Facade gate resumes still skip admission: they replay the checkpoint's pinned models, and runs launched before this change have no admission row, so a native-style resume check would be stricter than what those runs were started under. |
| `prepare_prompt_manifest` | Frozen prompt manifest | Was missing (every wizard launch failed) | Fixed earlier on this branch (F-01) |
| `apex_actor_id` / `langgraph_auth_user_id` stamps | Usage `consumer_id`, gate decision actor fallback, phase usage attribution | **Missing for launches.** Gate resumes stamp `apex_actor_id` themselves. | **Open decision** (below) |

Smaller notes:
- An admission is committed before later launch checks run. A launch refused after admission leaves a `model_run_admissions` row for a thread that has no run, exactly as the native hook does.
- Explicit `model_by_phase` entries for phases a person completes are dropped from the pinned map, because only agent phases use a model.
- The unit-test stand-in for admission (`tests/unit/conftest.py`) returns only the caller's explicit choices. Production pins every agent phase, which `tests/unit/test_facade_model_admission.py` covers against SQLite.

## Open decision: launch attribution

Facade launches carry no actor. As a result, usage events from those runs have no `consumer_id` (`services/usage.py`), phase usage invocations have no consumer (`phase_subgraph.py`), and gate decisions taken under auto policy fall back to "unknown" (`gates.py`). Native launches stamp the consumer ID, and so do facade gate resumes.

Stamping the launcher's consumer ID would close the gap. It conflicts with `test_launch_does_not_persist_raw_idempotency_key_or_principal`, which asserts the launch principal never appears in persisted thread or run rows. The principal is also an input to the deterministic launch thread ID, together with the idempotency key. Note that `model_run_admissions.actor_id` already stores the consumer ID per thread, in the application database rather than in the runtime's run rows, for both native and facade launches.

The options:

1. **Stamp the consumer ID**, the same as native launches and facade resumes, and narrow that test to the idempotency key.
2. **Stamp an opaque actor reference**, for example a keyed hash of the consumer ID that the usage and actor-name services can resolve. This keeps the principal out of run rows.
3. **Leave launches unattributed**, and document that facade-launched usage and auto-gate actors are anonymous.
