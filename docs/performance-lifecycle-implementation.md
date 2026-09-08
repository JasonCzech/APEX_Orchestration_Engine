# Performance lifecycle implementation status

This change delivers trust fixes, an opt-in cycle workspace, source and script capture, report evidence review, tracked actions and next-cycle draft reuse. **It does not complete the accepted lifecycle plan.** Keep the cycle feature disabled in production until the remaining execution, evidence, policy, and workspace workflows are implemented and validated.

## Implemented

- Missing imported outcomes remain unknown through graph seeding, model validation, reporting context, public projections, dashboard results, comparison, and timeline.
- Historical imported passes without recorded explicit-outcome provenance are projected as unverified. Original checkpoints are not rewritten.
- LoadRunner completion requires an explicit passed SLA status to project a pass. Unknown SLA status remains unknown; failed or aborted execution remains a failure.
- Missing live metrics are nullable. APEX Load omits unavailable summary KPIs instead of synthesizing zero. Measured zero remains valid.
- Requesting the Anthropic backend without its key fails with an actionable configuration message in every environment. Explicit stub selection remains supported.
- Newly produced phase results carry stub, LLM, simulator, live-provider, or imported provenance. Phase details display that provenance. Older results display “Not recorded.”
- Migration `0033` adds durable test cycles without changing pipeline checkpoints. A cycle records project/application/environment, owner, simulation/live selection, revision, and deliverable version history.
- `/v1/cycles` supports scoped listing, creation, detail, new versions, review, and reversible archival. SQL compare-and-swap requires the expected cycle revision for every version mutation and review.
- Content versions have checksums, author and creation time, exact upstream version dependencies, replacement lineage, and timestamped review history. Editing creates a new draft. Archived and superseded versions cannot receive new approval. Upstream changes qualify dependent approvals as no longer current, transitively, while preserving historical review records.
- Independent review, when enabled by deployment policy, requires a different authenticated consumer from the author. Clients cannot weaken this setting in a create or review request.
- The cycle dashboard is available at `/cycles` when the server advertises the `cycles` feature. It includes cycle creation, named application/environment selection, lifecycle navigation, structured deliverable editing, exact-version review actions, version comparison, upstream references, and executive/technical report views.
- The API supplies the engineering field definitions and validates required sections at submission and approval. Incomplete drafts remain saveable. Unsupported legacy fields and incomplete legacy approvals cannot silently qualify for current approval.
- Review actions are computed for the authenticated caller. The dashboard retains edits after revision conflicts and guards unsaved navigation without bypassing the existing one-time API-key safeguard.
- Compact navigation now lives inside the sticky header, avoiding the previously obscured navigation trigger.
- The initial `apex` CLI uses the same API for cycle creation/list/status, version import/export, review, and archival. It does not launch engines or publish externally.
- OpenAPI and the generated TypeScript client include the additive API operations.

## Development setup

Apply `alembic upgrade head` using the repository's normal database configuration before running this backend. The runtime readiness check expects migration `0038`, including when the cycle feature is disabled.

Set `APEX_CYCLES_ENABLED=true` to expose the experimental cycle API. Default is false. Set `APEX_CYCLES_INDEPENDENT_REVIEW=true` to require a distinct authenticated consumer; default is false. This is currently deployment policy, not the planned per-project policy system. Policy is captured at cycle creation.

Install/sync the Python project to obtain the `apex` entry point. Configure `APEX_URL` and `APEX_API_KEY` in the operator's environment. HTTPS is required except on loopback; redirects do not receive credentials. No automatic mutation retries occur.

```sh
apex --json cycles
apex create --name 'Checkout baseline' --project-id PROJECT --app-id APP --environment-id ENV
apex status CYCLE
apex import CYCLE draft-version.json
apex review CYCLE --version VERSION --revision 2 --action submit
apex review CYCLE --version VERSION --revision 3 --action approve
apex export CYCLE
apex review CYCLE --version VERSION --revision 4 --action archive
apex review CYCLE --version VERSION --revision 5 --action restore
```

`import` accepts a `VersionCreate` JSON document: `expected_revision`, `kind`, `title`, `content`, `dependencies`, optional `replaces`, and `change_summary`. Kinds are scope, test_plan, readiness, script, report, and postmortem. These use server-defined engineering sections and review validation. **They are not execution launch authority.** Script validation currently checks platform/format compatibility and native-JSON syntax, not provider acceptance or execution safety. Content is bounded; large evidence belongs in the artifact store. Export writes to stdout; use shell redirection explicitly to save a file.

Exit codes: 0 success; 2 invalid input/configuration; 3 missing identity or authentication/authorization rejection; 4 revision conflict; 5 transport or other API failure. `--json` is a global option for successful structured output; errors go to stderr. After ambiguous transport failure, query cycle status before retrying a mutation.

## Remaining implementation, in delivery order

The accepted plan is **not complete**. These items remain after the continuations below:

1. Full capability preflight and the LangGraph cycle coordinator, including both engines, immutable executable bindings, capacity/target isolation, native-schedule reconciliation and operational recovery. APEX Load's inspected create/export contract cannot currently prove immutable executable equivalence; dispatch remains blocked.
2. A unified source picker combining Jira Align requirements, Jira defects, uploads and the delivered Confluence page capture; typed requirement/scenario traceability; source refresh review across providers. Confluence currently supports a configured space, bounded title lookup and readable revisioned page text; macros and attachments remain explicit blockers.
3. Automated cycle readiness, expected-versus-observed checks, parameter/dependency-file capture, LoadRunner bundle import, source editing and AI-proposed revisions, and per-project approval policy.
4. Raw measurement/artifact verification and report evidence policy; destination previews and publication approval ledgers; Confluence publication, Jira defect deduplication/reconciliation and publication recovery. Current frozen reports capture safe engine-history summaries only.
5. Explicit baseline promotion, template proposals and complete CLI/API/dashboard parity for unfinished capabilities. Tracked actions and next-cycle draft reuse are implemented, but do not make provider readiness or source freshness current.
6. A seeded connected lifecycle with the deployment/provider targets requested from the user, database/restart/concurrency checks, measured accessibility across all shipped themes/layouts, and moderated engineer/reviewer release sessions.

## Validation boundary

Automated checks cover outcome preservation, historical qualification, unknown metrics, provider outcome parsing, version transitions, stale dependencies, distinct-principal review, archival, SQL revision predicates, route authorization, CLI request binding, and dashboard unknown-outcome display.

No live engine execution, Confluence publication, external defect creation, or moderated user sessions were performed for this change. The migration was checked through schema-head contracts; a real database migration and concurrent-worker lifecycle still need connected-environment validation. The cycle feature has no dispatch or publication endpoint and continues to expose preflight blockers, so deliverable approval cannot be mistaken for a completed operational preflight.

Latest verification: **5,479 backend/conformance tests passed, 13 skipped; 821 dashboard tests passed; 44 event-contract tests passed.** Python lint/format and Pyright passed; dashboard production build and contract lint/type checks passed. Backend tests reported the existing Starlette/httpx deprecation warning.


## Workspace verification (continuation)

The new workspace was inspected in a browser with an isolated, read-only API fixture. The plan view and editor rendered with catalog names, simulation labeling, next action, version identity and labeled workload controls. Desktop layout had no horizontal overflow. The inspection exposed the obscured compact-navigation trigger; the button was moved into the header. The full 390px/768px, zoom, contrast and screen-reader release matrix still needs completion. Screenshot evidence: `/tmp/apex-cycle-workspace-evidence/structured-plan-editor.png`.

Focused tests cover exact-version approval requests, retained edits after HTTP 409, unsaved navigation, successful draft-save announcements, hidden unauthorized actions, and coordination between draft and API-key navigation safeguards. No live execution or publication was performed.

Continuation regression results: **5,486 backend/conformance tests passed, 13 skipped; 827 dashboard tests passed; 44 contract tests passed.** The production build and type checks passed. Dashboard lint has 22 existing Fast Refresh warnings and no errors. The final draft-recovery/save-announcement changes also passed the six focused workspace/navigation tests.


## Execution preparation (continuation)

Migration `0034` adds immutable execution preparations and links to existing engine attempts. The dashboard Executions workspace and CLI use the same scoped API:

- `GET /v1/cycles/{cycle_id}/executions` returns recorded bindings, freshly evaluated local prerequisites, safe connection choices, and related runs. It includes up to 50 recent run candidates; each cycle is limited to 100 preparations and 100 links.
- `POST /v1/cycles/{cycle_id}/executions` freezes approved plan/script checksums, exact source checksum, workload numbers, target generation, connection generation, mode and intended launch owner. Scripts and readiness must reference the selected plan. An idempotency key replays the same preparation; different contents with that key return conflict. Concurrent writes require the cycle revision.
- `POST /v1/cycles/{cycle_id}/runs` links a known engine attempt only when both project and application ownership are established. Links remain explicitly **bindings unverified** and do not rewrite checkpoints or claim script/workload equivalence.

Preflight flags superseded approvals, changed target/connection generations, missing numeric SLAs, unsupported arrival-rate workloads, and stale readiness. The initial inventory freshness limit is 24 hours; readiness must reference the latest scan and current target version. Engineer-authored readiness remains an assessment, not automated verification of expected-versus-observed configuration. Connectivity, capacity and provider script validation remain Not checked. Artifact-backed provider binding and cycle dispatch remain Blocked. No live or simulated execution is launched by these operations. Sequential execution is recorded as intended policy; its dispatch enforcement and native-schedule reconciliation still need implementation.

Optional structured plan fields `p95_ms_threshold` and `error_rate_threshold` hold overall execution SLA numbers separately from transaction-specific prose. Readiness adds `inventory_snapshot_id` and `target_version`. Non-finite numbers cannot pass content review.

```sh
apex --json executions CYCLE
apex prepare-execution CYCLE execution-preparation.json
apex link-run CYCLE ENGINE_RUN_ID --revision 8
```

Preparation JSON uses `expected_revision`, a stable `idempotency_key` (at most 32 characters), `plan_version_id`, `script_version_id`, optional `readiness_version_id`, optional `connection_id`, and `launch_authority` (`pipeline` or `native_schedule`). Simulation must omit a live connection. A native launch owner is available only for APEX Load. Keep the same idempotency key when retrying an ambiguous request. The returned preparation ID identifies the saved operation; GET shows its current blockers. Dashboard retries preserve the key while the form remains mounted; after leaving/reloading, inspect existing preparations before creating another.

Verification: **5,523 backend/conformance tests passed, 13 skipped; 829 dashboard tests passed; 44 event-contract tests passed.** Focused post-review tests cover immutable binding contents, stale scans and generations, superseded approvals, retry identity, run scope, unknown binding status and role-limited controls. Browser inspection used an isolated read-only fixture, with no live integrations. Database migration execution, concurrent-worker integration tests and the complete accessibility release matrix remain outstanding.

Final checks: production build, Pyright, Python lint/format and dashboard lint passed (22 existing Fast Refresh warnings). The final execution UI tests passed after the layout review. Browser widths of 628px (initial layout) and 1422px (final styled layout) had no horizontal overflow. Final screenshot: `/tmp/apex-cycle-execution-evidence/executions-final.png`.


## Approved script source storage (continuation)

Migration `0035` adds `cycle_script_artifacts`: a durable intent per cycle/version, original storage connection and generation, approved-content checksum, source-byte checksum, size, author and verification timestamp. Storage is bound to an explicit enabled S3-compatible connection in the cycle project or a global connection. Process-local stub storage and static fallback are rejected.

The script editor accepts UTF-8 `.json`, `.c`, and `.txt` files into a new draft, preserving source text. Files are bounded to 150,000 bytes and 50,000 characters; binary bundles are not accepted by this source editor. Direct edits/imports still require renewed exact-version approval. Native JSON and LoadRunner Web HTTP/HTML text can then be stored; external import references must first be materialized and remain unsupported by this operation.

- `GET /v1/cycles/{cycle_id}/scripts` lists source-storage records and safe connection choices.
- `POST /v1/cycles/{cycle_id}/scripts/{version_id}/artifact` verifies current approval, commits a stable storage intent, writes the approved bytes through the artifact-store port, validates the acknowledgement, then reads back and hashes the bytes before recording success. The first reservation requires the current cycle revision. Retrying the same version uses its original binding; changing stores or source under that identity returns conflict. A verified object is read and checked on retry, never overwritten by that retry.
- `GET /v1/cycles/{cycle_id}/scripts/{version_id}/content` authorizes the cycle, resolves the recorded connection generation and performs a bounded checksum-verified download. Missing/tampered objects or changed storage connections return an actionable failure. Provider error details are not reflected.
- New execution preparations capture the matching verified artifact ID. Older preparations retain their original snapshots and continue to request a stored source. The storage prerequisite describes a historical verification, not current provider readiness.

```sh
apex --json scripts CYCLE
apex store-script CYCLE VERSION --connection STORE --revision 9
```

Pending storage intents survive errors and ambiguous responses. Retry the same version and connection to resume. Replays do not require a new cycle revision. There is no cleanup/deletion operation for these retained evidence objects. Administrative object mutation remains detectable through checksum verification; object-store retention/WORM policy has not been added.

This increment verifies **APEX source storage only**. It does not validate native engine DSL semantics, assemble LoadRunner bundles, upload into either execution provider, verify parameter/dependency files, or enable dispatch. Artifact-backed provider import, exact provider-version binding, the cycle coordinator, source retrieval, publication and next-cycle actions remain outstanding.

Verification: **5,553 backend/conformance tests passed, 13 skipped; 832 dashboard tests passed.** Tests cover byte-exact UTF-8 storage, invalid acknowledgements, tampering, bounded stream closure, connection scope/generation/static fallback, retry behavior, sanitized failures, file import and viewer restrictions. No live object-store write, provider execution, real database migration or publication was performed by this development task.


Production build, Pyright, Python lint/format, and 44 contract tests passed. Dashboard lint reported the existing 22 Fast Refresh warnings and no errors. An isolated read-only browser fixture at 628px showed storage identity, verification time and actions without horizontal overflow. The full accessibility matrix remains outstanding. Screenshot evidence: `/tmp/apex-cycle-script-evidence/script-storage.png`.


## Provider source validation (continuation)

Migration `0036` adds durable source-validation attempts linked to an immutable execution preparation. The dashboard's prepared-execution cards and operator CLI use the same scoped operations:

- `GET /v1/cycles/{cycle_id}/executions/{preparation_id}/script-validations` returns capability blockers and attempt history.
- `POST` to the same path accepts `expected_revision` and a stable `idempotency_key`. It records a pending attempt before provider IO. A repeated key resumes pending work or returns its recorded result; using the key for another preparation returns conflict. Completed attempts are not overwritten. There are at most 200 attempts per cycle.

APEX Load validates the exact stored native JSON object through its draft-validation endpoint. The adapter does not generate defaults, upload a script, create a test, provision, schedule or start execution. The stored source is checksum-verified before transmission, and both artifact and engine connections retain their recorded scope/generation. Source validation supports up to 150,000 bytes without changing the existing pipeline's smaller script-reference limit or its inline-script execution guard.

The current LoadRunner adapter cannot validate imported source; its local test-ID checks are not treated as that capability. LoadRunner and simulation preparations expose explicit blockers instead of falling back to a stub. Missing/malformed provider outcomes, connection failures and interrupted checks never become acceptance.

Attempt states are Pending, Accepted, Rejected and Unavailable. **These are source-validation states, not test outcomes.** An acceptance is current only when it is the latest attempt, no more than 24 hours old and the source/plan approvals and recorded connections remain current. This initial freshness window is not yet project-configurable. A newer attempt, even pending or rejected, makes an older acceptance historical. Object availability is rechecked when validation runs; GET history does not perform live IO. Successful source validation does not certify workload, target, capacity, immutable provider import or launch readiness.

The existing APEX Load execution adapter also now correctly rejects a provider response of `valid: false` with an empty or omitted issue list. Previously, the empty list could produce an apparent validation success. Regression fixtures cover both inline and named-script paths.

```sh
apex --json script-validations CYCLE PREPARATION
apex --json validate-script CYCLE PREPARATION --revision 10 --idempotency-key OPERATION_KEY
```

Reuse the operation key after an ambiguous response or interrupted pending attempt. Use a new key to deliberately perform another completed validation. For `validate-script`, exit 0 means current source acceptance, 6 means rejection, and 7 means unavailable or historical acceptance. Existing input/authentication/conflict/transport exit codes remain unchanged. No return code authorizes launching a test.

Verification: **5,582 backend/conformance tests passed, 13 skipped; 835 dashboard tests passed.** Focused coverage includes explicit provider booleans, rejection without diagnostics, unchanged source payloads, no resource-creation calls, interrupted-attempt recovery, obsolete acceptance, scope/capability blockers, viewer restrictions and CLI operation identity. Live provider checks, real database migration, provider upload and cycle dispatch still require connected-environment implementation/validation.

Final provider-validation checks: production build and Pyright passed; Python lint/format passed; 44 event-contract tests passed. Dashboard lint retained 22 existing Fast Refresh warnings with no errors. An isolated read-only browser fixture at 628px displayed provider rejection and its diagnostic without horizontal overflow. Evidence: `/tmp/apex-cycle-validation-evidence/provider-rejection.png`. The final CLI/type and metadata-sanitization changes also passed 19 focused backend tests.

## Existing APEX Load library import (continuation)

Migration `0037` adds provider-export provenance linked to each imported draft. The Scripts workspace now has an expandable library picker with named connections, paginated script choices, a source preview and fingerprint, approved-plan selection, and parameter/dependency and requirement-coverage fields. Viewers can inspect exports; operators can capture them as new reviewable drafts. Conflicts preserve form inputs. Successful import links to the exact new version.

- `GET /v1/cycles/{cycle_id}/provider-scripts` lists authorized connections and prior imports. An explicit `connection_id` reads its configured provider project; adding `remote_script_id` previews one export. `offset` and `limit` paginate the bounded library projection.
- `POST /v1/cycles/{cycle_id}/provider-scripts/import` accepts the preview's connection generation and SHA-256, a current approved plan version, expected cycle revision, and stable idempotency key. It re-reads the export and rejects changed content before saving. Draft and provenance are committed atomically under the cycle revision check. Replaying a successful request returns its original draft without provider IO.
- Provenance retains provider script/name/project/version, connection generation, export checksum, authenticated importer and capture time. Content remains subject to normal draft review, approval, artifact storage and source validation.

**This captures a provider export, not a verified executable provider version.** Inspection of the available APEX Load source showed that library exports can be disclosure projections, while ordinary script creation can overwrite existing IDs. Neither behavior establishes immutable executable equivalence. The new API therefore explicitly reports `content_scope: provider_export` and `executable_content_verified: false`. It rejects explicit redaction markers, credential material, inconsistent identities, malformed or oversized exports, incomplete libraries and unsupported connection scopes. Simulation never contacts the live library. No provider resource is created or launched.

```sh
apex --json provider-scripts CYCLE
apex --json provider-scripts CYCLE --connection CONNECTION
apex --json provider-scripts CYCLE --connection CONNECTION --script REMOTE_SCRIPT
apex import-provider-script CYCLE import.json
```

The import document follows the generated `ImportProviderScript` schema: `expected_revision`, `idempotency_key`, `connection_id`, `connection_version`, `remote_script_id`, `expected_source_checksum`, `plan_version_id`, `title`, `parameters`, and `coverage`. Reuse its idempotency key after an ambiguous response; a different logical request requires a different key. JSON output and authorization use the same domain API as the dashboard.

Immutable provider creation and executable fingerprints, LoadRunner library/bundle import, parameter-file capture, exact provider execution bindings and the cycle coordinator remain outstanding. The existing dispatch blocker remains in place. Deploy schema/contracts before consumers; the cycle feature remains opt-in. No live provider requests or database migrations were performed during this increment.

Verification: **5,605 backend/conformance tests passed, 13 skipped; 837 dashboard tests and 44 contract tests passed.** Coverage includes preview drift, atomic draft/provenance saves, optimistic-concurrency conflicts, idempotent replay, connection scope/generation, malformed exports, viewer restrictions, retained form details and CLI query encoding. Production build, Pyright and Python lint/format passed. Dashboard lint retained 22 existing Fast Refresh warnings, with no errors. A read-only browser fixture at 628px displayed named choices, source fingerprint, explicit unverified status and draft inputs without horizontal overflow. Evidence: `/tmp/apex-provider-import-evidence/provider-preview.png`. Live provider checks and the full accessibility/release matrix remain outstanding.

## Cycle result review and descriptive comparison (continuation)

The Results & Reports workspace now reads linked execution attempts through `GET /v1/cycles/{cycle_id}/results`. It shows engine/attempt identity, execution status, explicit test outcome, summary availability, conventional KPIs, SLA breaches and evidence qualifications. The endpoint uses the cycle's project/application authorization and requires known ownership on each joined run. A comparison baseline must be one of those accessible linked attempts; an unrelated baseline returns 404. This is an additive read-only operation and needs no migration beyond the existing cycle/link tables.

A completed execution never supplies a missing test outcome. Invalid summaries or conflicting engine identities are withheld. A recorded pass with SLA breaches is displayed as failed with a conflict explanation; incomplete, failed or aborted execution cannot display a recorded pass. Missing, malformed, negative or out-of-range conventional metrics remain unavailable, while measured zero remains zero. These are best-effort engine-history projections, not fresh provider telemetry or frozen report evidence. Retrieval time is labeled accordingly. Linking an attempt still does not prove its script/workload/environment bindings to the cycle.

Operators can select a baseline attempt and read absolute and percentage KPI differences. Comparison is restricted to completed attempts from the same engine. Zero baselines, missing metrics and non-finite percentage calculations produce unavailable percentages. Equal values are labeled equal; higher/lower preferences are contextual and no difference is called an improvement. Selecting a comparison does not promote a baseline or change cycle records. Raw measurements, report drafts and approval history remain unchanged.

```sh
apex results CYCLE
apex --json results CYCLE --baseline ENGINE_RUN_ID
```

The CLI uses the same endpoint and identities, and reports execution status separately from test outcome. Successful command execution indicates successful retrieval, not a passing performance test. Existing execution-view links remain run-level navigation; engineers must match the linked attempt when inspecting run history.

Still outstanding: immutable executable provider bindings and cycle dispatch, native-schedule reconciliation, transaction-level evidence and workload-equivalence validation, frozen evidence attached to report versions, publication, baseline promotion and next-cycle reuse. This increment does not make the entire lifecycle release-ready.

Verification: **5,628 backend/conformance tests passed, 13 skipped; 839 dashboard tests and 44 contract tests passed.** Build, Pyright, Python lint/format and diff checks passed. Dashboard lint retained 22 existing Fast Refresh warnings with no errors. A read-only browser fixture at 628px confirmed distinct completed/failed status, explicit equal and higher/lower comparisons, unavailable metrics and no horizontal overflow. Screenshot: `/tmp/apex-cycle-results-evidence/comparison.png`. Live providers, real authentication and the full accessibility/user-study release gates remain unvalidated.

## Frozen report-summary evidence (continuation)

`POST /v1/cycles/{cycle_id}/reports/{version_id}/evidence` captures selected, accessible linked engine-history summaries into a **new report draft**. The request contains `expected_revision`, `idempotency_key`, and `expected_results` (engine-run ID to `evidence_checksum`, taken from the results preview). A changed summary, inaccessible attempt, changed cycle, superseded report, stale dependency or oversized snapshot rejects capture. Up to 20 attempts fit a request, subject to the existing content/aggregate size limits.

The server writes `_execution_evidence` into the new report content, including immutable projected metrics/outcomes/qualifications, capture time, authenticated actor and operation identity. It participates in the existing content checksum and version review. Capture and revision update use the same atomic compare-and-swap as other deliverable writes. Replaying an identical operation returns the recorded cycle without re-reading engine history. Old report content and reviews remain intact; new drafts do not inherit approval. Editing interpretation preserves the snapshot exactly. Ordinary version creation cannot forge, edit or remove this reserved evidence content. Capturing refreshed evidence creates another draft.

The dashboard displays frozen outcomes and metrics with the selected report version, with technical provenance under a disclosure. Operators can capture the current preview's linked attempts (up to 20) into a new draft. The CLI supports an explicit subset using the same request schema:

```sh
apex --json results CYCLE
apex --json freeze-report-evidence CYCLE REPORT_VERSION evidence-request.json
```

This is a snapshot of the safe **history projection**, not a copy or verification of raw provider artifacts. Unknown outcomes, partial measurements and unverified execution bindings remain qualified. Existing manually written reports retain their previous review behavior; future publication must enforce its evidence policy separately. No external publication, raw-artifact freezing, migration, engine dispatch or baseline promotion is included here.

Browser verification also found duplicate React keys between sibling version panels. Script storage, report evidence and version details now use distinct keys so refreshing a cycle cannot duplicate panels. A full-page report refresh regression supplements the component-level capture tests.

Verification: **5,638 backend/conformance tests passed, 13 skipped; 842 dashboard tests and 44 contract tests passed.** An additional six focused evidence tests passed after adding interpretation-preservation coverage. Build, Pyright, Python lint/format and diff checks passed. Dashboard lint retained 22 existing Fast Refresh warnings and no errors. The corrected browser fixture displayed exactly one frozen-evidence panel at 1422px, without horizontal overflow; screenshot: `/tmp/apex-report-evidence-verification/frozen-report.png`. No live provider, publication or database mutation was exercised by the browser fixture. The full accessibility and connected lifecycle release gates remain outstanding.

## Report review integrity and evidence comparison (continuation)

Selecting “Compare with version” on a report now compares frozen execution evidence as well as interpretation fields. Reviewers see added/removed/changed attempts, before/after outcomes and metrics, changed SLA breaches and qualifications, and changed execution timestamps. Unknown values remain unavailable and measured zero stays zero. Capture metadata and metric ordering alone do not count as changed execution evidence. Missing or malformed snapshots are explicitly unreadable; they are not treated as unchanged evidence or as confirmed removals. This view works in both executive and technical report modes.

Review correctness also now checks the stored content checksum. A version with altered content cannot be submitted or approved, cannot retain a current approval, and invalidates dependent approvals. API projections explain the checksum mismatch and withhold review actions. Creating a replacement from corrupted content is rejected too, preventing a new checksum from legitimizing untrusted inherited evidence. Existing records are not rewritten automatically; retained originals must be repaired through an authorized recovery process, or a fresh draft created from independently verified inputs.

This remains local APEX report review. Raw artifact validation, evidence-enforcing publication policy, external publication and the broader connected lifecycle remain outstanding.

Verification: **5,643 backend/conformance tests passed, 13 skipped; 846 dashboard tests passed**, followed by six focused comparison tests covering the final unreadable-snapshot and ordering refinements. Production build, Pyright, Python lint/format and diff checks passed. Dashboard lint retained 22 existing Fast Refresh warnings with no errors. Browser inspection at 628px showed explicit unknown-to-failed and unavailable-to-measured changes without horizontal overflow: `/tmp/apex-report-review-evidence/evidence-diff.png`. No live provider or publication operations were performed.

## Tracked postmortem work and next-cycle reuse

Migration `0038` adds cycle action history and idempotent next-cycle lineage. `POST /v1/cycles/{cycle_id}/followups` records title, owner, status (Open, In progress, Done, Cancelled), notes and authenticated change history. Requests use expected cycle revision and an operation key; identical retries return recorded state, conflicting reuse fails. The dashboard preserves entries on conflicts and guards unsaved navigation. Actions are engineering records, not infrastructure mutations.

`POST /v1/cycles/{cycle_id}/next-cycle` creates a child in the same project/application/environment and mode. It validates the target, preserves or strengthens independent-review policy, and atomically reserves the parent revision with child creation. The parent/key uniqueness constraint prevents duplicate children. Selected active, intact scope/plan/script versions must include their full upstream dependency closure. Copies receive new IDs, draft status, empty reviews, remapped dependencies and original-version provenance. Script validation claims are reset. Readiness, executions, artifact/provider bindings and report results are not copied. Selected unresolved actions return as open work with original-action lineage. Fresh source, environment, provider and approval validation remains required.

```sh
apex --json followup CYCLE action-request.json
apex --json next-cycle CYCLE next-cycle-request.json
```

Use `FollowupWrite` and `NextCycleRequest` from the generated schema. The dashboard Postmortem workspace supports both operations, displays owners/status and links the created cycle. Existing cycles and reports remain intact. Caps: 200 actions, 100 history events per action, 1 MB action aggregate, and up to 50 selected reused versions subject to normal cycle size limits. Migration SQL was generated in PostgreSQL offline mode; no real database migration was executed.

## Confluence Cloud scope capture

The `confluence` document adapter uses the existing resolver, stored secrets, bounded HTTP transport and document port. Configure an enabled `documents` connection with `provider: confluence`, an HTTPS `<site>.atlassian.net` `base_url`, `user_email`, numeric `space_id`, and an API-token `secret_ref`. The adapter uses [Confluence Cloud REST API v2 pages](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-page/) for bounded page-title lookup and revisioned retrieval. It does not follow redirects or arbitrary page URIs. Site/space/page/version mismatches, permission errors, absent bodies, unsupported macros and attachments are explicit failures. Readable storage content becomes plain text; script/style content is not included. Page title and revision are preserved in `DocumentContent`/`DocRef`.

`GET /v1/cycles/{cycle_id}/sources` lists accessible Confluence connections, supports exact-title lookup (up to 20 results), and previews a selected page revision with a content fingerprint. `POST /v1/cycles/{cycle_id}/scopes/{version_id}/sources` re-fetches that revision, checks fingerprint and connection generation, and captures page text and provenance under `_source_evidence` in a **new scope draft**. Up to 32 sources are retained, within the existing 150 KB version limit. Larger pages require a future artifact-backed source capture flow. Re-capturing a page replaces it only in the new draft; original scope versions remain unchanged. Ordinary scope edits cannot forge, edit or remove the reserved snapshots. Renewed scope approval and downstream review are required. Simulation does not contact live providers.

```sh
apex --json sources CYCLE
apex --json sources CYCLE --connection CONNECTION --title 'Exact page title'
apex --json sources CYCLE --connection CONNECTION --page PAGE_ID --revision REVISION
apex --json capture-source CYCLE SCOPE_VERSION capture-request.json
```

`CaptureSource` binds `expected_revision`, `idempotency_key`, connection ID/generation, exact `DocRef`, expected content checksum and an operator label. The stored authoritative title comes from the provider response. Identical retries return the recorded capture without provider IO. Scope browsing/capture and read-only previews are available in the dashboard; the existing API authorization remains shared with CLI. This does not yet combine Jira requirements/defects/uploads into one picker or freeze raw wiki attachments. No live Confluence reads or writes were performed during implementation.

Verification for this backlog pass: **5,682 backend/conformance tests passed, 13 skipped; 853 dashboard tests and 44 contract tests passed.** Build, Pyright, Python lint/format and diff checks passed. Dashboard lint retained 22 existing Fast Refresh warnings with no errors. Migration `0038` produced valid offline PostgreSQL upgrade SQL; the live database remains untouched. Isolated read-only browser fixtures at 628px showed Confluence revision selection/readable text and tracked action ownership/status without horizontal overflow. Evidence: `/tmp/apex-remaining-work-evidence/confluence-preview.png` and `/tmp/apex-remaining-work-evidence/postmortem-actions.png`. Connected provider targets were requested for release testing; the implementation does not claim those live gates or the remaining backlog are complete.
