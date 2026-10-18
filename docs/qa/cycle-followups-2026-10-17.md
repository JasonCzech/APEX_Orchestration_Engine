# Cycles follow-ups — 17 October 2026

This continues the [usability triage](usability-triage-workflows-2026-10-17.md) in APEX Orchestration, starting from main `10c7eee`. APEX Load source was not changed. An existing Load binary was used as an isolated local provider, with a disposable database and a task-owned HTTP target.

## Delivered behavior

| Follow-up | Implementation |
|---|---|
| F1 — authoritative display names | A permission-filtered project label catalog supplies names to presets, application/environment navigation, project choices and Cycle cards. Project-wide administrators can edit or remove a label without changing IDs or access. A bounded Cycle actor lookup resolves only identities already recorded on an authorized cycle. Duplicate names retain full copyable IDs; absent, deleted or denied names have explicit fallbacks. |
| F2 — Cycle execution | An explicit, confirmed launch dispatches an approved preparation through the existing checkpointed pipeline and engine lifecycle. Its source, workload, target, connection, author and operation identity remain bound to the preparation. Durable receipts support refresh, uncertain-response recovery, cancellation before pipeline binding, and reconciliation of a bound intent. Automatically dispatched runs participate in history, result comparison and report-evidence capture with independently checked binding receipts. |
| F3 — list progress | The existing Cycle list returns a bounded six-deliverable summary, persisted with every version mutation. Cards display current approval counts and expose each deliverable state without requesting every Cycle detail. Missing legacy projections are backfilled in small authorized batches with a revision check; unreadable legacy history stays explicitly unknown. |

F2 initially supports **live APEX Load native HTTP execution**. It does not enable every engine or source format. Unsupported capabilities remain visible launch blockers.

## Execution contract

Enable `APEX_CYCLES_ENABLED`, configure an enabled APEX Load connection with native control and an explicit provider project, configure the approved source artifact store, and use an enabled persisted operator consumer with the required scope. Bootstrap/development identities cannot own a durable launch. Apply migrations **0040 and 0041** before deploying the new API/dashboard contract.

A launch requires current approved plan, script and readiness versions, the exact stored source artifact, a current target and connection generation, and a successful current source validation from the last 24 hours. The approved source must explicitly declare native HTTP, match the environment base URL, restrict `metadata.runtime_policy.allowed_target_hosts` to that hostname, and disable redirects with `max_redirects: -1`. Actions must remain within that origin. The source cannot contain credentials, redacted placeholders, external data/parameter files, protocol mixtures or unreviewed URI overrides. Unsupported arrival-rate workloads, native-schedule ownership, mapped releases and other engines remain blocked with reasons.

The adapter uploads the approved executable content without regenerating its actions. It reads the stored provider source back and verifies equivalence, permitting only identified empty serialization fields and storage metadata. Recovery reuses the existing scoped test and its original source ID; it does not upload a new source on every retry. The adapter checks the provider's project, implicit legacy application/release context, workload and source again before starting.

Capacity is checked at launch, not reserved by a readiness preview. A durable unique reservation serializes **Cycle dispatches sharing an environment**; ordinary independently launched pipelines and external provider users do not share this reservation. The UI states that boundary. Ownership is released only after cancellation before pipeline binding, a recorded pre-provider failure, or terminal execution with confirmed cleanup. A provider completion does not establish a passing test assessment.

Refresh never starts an execution. An ambiguous submission keeps the same operation key. An unsubmitted intent can be canceled only if an atomic check wins before pipeline binding. Once bound, recovery reconciles the original intent; changed approvals or permissions must be rechecked before any provider effect. A new execution requires a new preparation. Public pipeline/native requests cannot forge a Cycle dispatch binding, and reruns cannot discard or change it to execute an unreviewed source.

The CLI exposes the same operations without automatic mutation retries:

```sh
apex execution-dispatch CYCLE PREPARATION
apex dispatch-execution CYCLE PREPARATION --revision REVISION --idempotency-key STABLE_KEY
apex cancel-dispatch CYCLE PREPARATION --dispatch-id DISPATCH
```

## Names and deliverable state

Project names are authored metadata, not names inferred from an application or UUID. Project/app scope controls reads; changing the name requires project-wide administrator access. Names can be identical and can be removed without deleting project resources. The Cycle actor projection is capped at 256 recorded identities and does not expose an administrative consumer directory. Full IDs remain available when names cannot be resolved.

Each kind uses its latest recorded version. Archiving it does not resurrect an earlier approval. Overall precedence is: no versions → Not started; only absent/archived versions → Deliverables archived; any stale approval → Approval stale; otherwise any pending review → In review; all six current approvals → Deliverables approved; otherwise In progress. Missing, draft and archived versions cannot count as approved. These labels describe deliverables, never execution success or release acceptance.

## Review and evidence

Review and test-driven fixes included exact-source retry adoption, native empty authentication serialization, implicit provider scope verification, environment reservation release, pre-submission cancellation races, changed-prerequisite recovery, graph prerequisite routing, bounded legacy projection backfill and complete actor attribution. The HTTP-to-compiled-graph regression intentionally does not seed invented predecessor phases.

The final cross-cutting review found and closed four additional issues before push: compatibility with the previous durable run configuration, unsupported Cycle rerun controls, stale list progress after source/report capture, and report copy that incorrectly called verified bindings unverified. The compatibility regression uses a serialized snapshot from `10c7eee` and replays both parent and phase checkpoints; only that exact historical shape receives an absent Cycle binding. Cycle runs expose a fixed capability reason rather than their private binding ID, and direct/stale-list preflight paths still consult the authoritative detail. Ordinary reruns remain available.

The isolated real provider probe recovered the original test after interrupted provisioning, started it, observed running/completed states and collected its archived metrics. Load reported partial telemetry (85% coverage) and an inconclusive assessment; Orchestration retained `passed: null`. A real disposable PostgreSQL database upgraded from 0039 through 0041, verified the new tables and schema head, and was dropped afterward. Neither check used the user's existing provider database.

Final validation of code commit `7172473`:

| Check | Result |
|---|---|
| Backend suite | 6,142 passed; 23 skipped under the default test configuration |
| PostgreSQL supplement | 37 passed in a disposable database, including 19 of those default skips |
| Dashboard unit suite | 1,146 passed across 139 files |
| Production-bundle Playwright | 93 passed; zero skipped, flaky or failed cases |
| Shared contracts | 48 passed; type and lint checks passed |
| Static/build checks | Ruff, Pyright, dashboard typecheck, production build and diff checks passed; dashboard lint has zero errors and 23 existing Fast Refresh warnings |
| Independent final review | No remaining findings after fixes and cross-cutting re-review |

Four default skipped cases remain: two require MinIO, one requires Linux memfd, and one covers a documented simulator abort no-op. The PostgreSQL supplement is additional evidence, not 37 newly unique tests to add to the main count. Both Python runs retain the existing Starlette/httpx deprecation warning.

The browser suite covers scoped operator/admin mutation workflows, viewer and unscoped operator refusals, desktop/mobile recovery and cancellation, stale-list rerun entry, ordinary rerun preservation, report binding states, and the existing route census/keyboard/theme checks. Its connected-mode scenarios use controlled HTTP fixtures. Separate real-provider and PostgreSQL results are distinguished in the [machine-readable verification receipt](cycle-followups-verification-2026-10-17.json).

The [launch confirmation](../../output/playwright/cycle-dispatch-mobile.png) and [names/progress](../../output/playwright/cycle-names-progress-mobile.png) captures were visually checked at 390px.

Task-owned provider/target processes and browser sessions are closed. Local disposable test/build caches were removed (17.9 MB); dependencies, deliverable builds, captures and application data were retained. This work used no Go or Docker builds and left their shared caches intact.

## Remaining integration boundaries

The local provider exercise is not certification of a production Load deployment, distributed runtime, LoadRunner, arbitrary DSL actions, native scheduling or mapped release execution. Confluence publication still has no write implementation; existing captured-source retrieval remains read-only and was not exercised against a live tenant. Production auth expiry, native screen-reader speech and the absent run-calendar surface remain outside this verification. These are explicit capability or environment boundaries, not claims that fixture responses proved external operations.
