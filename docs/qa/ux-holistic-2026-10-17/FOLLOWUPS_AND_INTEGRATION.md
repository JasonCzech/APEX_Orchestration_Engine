# Follow-ups and cross-product integration

18 October 2026 · state after `codex/holistic-ux-remediation-2026-10-17` @ `2b81afa`
(18 commits since the re-review, clean tree) · verified by rebuilding and driving the app.

---

# Part 1 — What remains for APEX Orchestration

## What the push actually closed

I re-tested the items I had left open. All four are genuinely fixed:

| Was open | Now |
|---|---|
| **L-01** every approval `actor: "unknown"` | `actor: "3ebe0a0f08cb4268a05cf563e6ed477b"` — the authenticated consumer id, recorded through the resume route |
| **ARE-07** rejected body → 500 | **422** with a useful detail: *"LangGraph assistant_search limit must be between 1 and 5"* |
| **Workstream 1** 42 × 12×6 px buttons on `/runs` | One `role="img"` per row, label *"Phase progress. Story Analysis — Complete (attempt 1); …"*, **0 focusable segments, 0 undersized targets** |
| **apex-light chart series < 3:1** | 3.01–3.25:1 in every theme, and the contrast checker now covers 8 series/panel pairs per theme as a regression test |

They also implemented workstream 5 before I finished writing it:

- `--font-sans: "ATT Aleck Sans", …` and `--font-mono: ui-monospace, SFMono-Regular, …` — **byte-identical to Load's stacks**, so the licence question resolved in favour of adopting it.
- Sidebar defaults to the **64 px icon rail** with overlay expansion, matching Load. Verified on a fresh profile: `class="sidebar collapsed"`, width 64, no stored preference.

The contrast checker now covers 127 token pairs per theme (originally 43).

## Remaining follow-ups

### 1. Identifiers are recorded correctly but displayed raw — P2

This is now the dominant open theme, and it shows up in three places that look unrelated but share one cause: **the public projections return IDs without their display names, so the UI either prints a UUID or omits the field.**

- **Approval actor.** The Reviews panel renders
  `prompt_review approve 3ebe0a0f08cb4268a05cf563e6ed477b Oct 17, 2026, 08:47:35 PM`.
  The governance fix is correct in the data; a 32-hex consumer id is not an answer to "who approved
  this". Resolve it to the consumer name, with the id behind the existing `CopyableId` affordance.
- **ARE-09 (their triage, "Open, partial").** Their own note: *"The remaining request to display a
  project name cannot be fulfilled by the current ID-only project con[text]"*.
- **ARE-21 (their triage, "Open, partial").** Own owner/author/reviewer IDs resolve to names only
  when they are *your own*; others stay as IDs.

**Recommendation.** Fix it once, at the projection layer: have `/v1` return `{id, name}` pairs for
consumer, project and application wherever it returns a bare identifier today, and let
`ApplicationLabel` (which they already built for the run-context case) generalise to all three.
Fixing this per-screen will take three times as long and drift again.

### 2. Gate kind still renders as a raw enum — P3

The same panel prints `prompt_review` where every other surface says "Review prompts". One label
map, already present elsewhere in `runDisplay.ts`.

### 3. Their four self-declared partials — P2/P3

Credit where due: their triage marks these open rather than claiming closure.

| ID | What remains |
|---|---|
| ARE-09 | Project **name** display — blocked on the ID-only project context (see follow-up 1) |
| ARE-16 | Cycle execution preflight — arrival-rate disclosure and archive landed; the rest is partial |
| ARE-21 | Cycle list progress/status absent — `CycleSummary` carries no bounded active-deliverable summary |
| ARE-27 | Inventory attribution and the application workspace — partial |

ARE-21 is the one worth scheduling: a cycle list with no progress column is the Cycles equivalent
of the bare `n/m` PROGRESS column we just fixed on `/runs`.

### 4. The live-activity promise survives for active runs — P3

`ActivityFeed.tsx:322` still carries *"Activity streams in while the run is executing"* on the
active-run branch. For connected runs that finish in milliseconds and for demo mode — where the
bundled transport emits no stream at all — this promises something that never arrives. Make the
copy conditional on the transport being able to attach, not on run state.

### 5. Still unverifiable here — not defects

Walkthrough **m** (Load handoff and return) and live SSE reconnect remain unexercised: this
environment has no configured Load console URL and the simulated engine completes phases faster
than the stream can attach. Both need a real Load deployment to close.

---

# Part 2 — Integration landscape across Load and Orchestration

## The shape of the relationship

The integration is **one-directional and radically asymmetric**, which is the single most important
fact about it:

| Direction | Surface |
|---|---|
| Orchestration → Load | `adapters/apex_load/engine.py` **3,547 lines**, `ports/apex_load_control.py` with ~25 control-plane operations, a 902-line workspace page, 8 backend routers |
| Load → Orchestration | `orchestrationReturn.ts` **16 lines** + `OrchestrationReturn.tsx` **10 lines** |

Load's entire knowledge of Orchestration is a URL validator: it accepts an `orchestrationReturn`
parameter, requires HTTPS or loopback, rejects credentials and fragments, allows only
`/runs/{id}` or `/apex-load` paths and three query keys, and renders a return link. It never calls
Orchestration, never trusts it, and would behave identically if Orchestration did not exist.

Orchestration, meanwhile, is a **full control-plane client** of Load — scenario CRUD, schedule CRUD,
schedule-approval decisions, run calendar, native run listing and scanning, run summaries, metrics,
error evidence, artifact collection and console URLs.

So the UX contract's description — *"This is navigation context, not authorization or proof of a
configured integration"* — accurately describes the **shallow** bridge while a **deep** API bridge
sits beside it, undocumented in that contract. That gap between the two bridges is where most of
the problems below live.

## Complementary — keep both

These exist in exactly one product and should stay there.

**Orchestration only**
- Work management: Jira (1,070 lines), Jira Align (898), Confluence (210)
- The agentic layer: 7-phase pipeline, prompt catalog and versioning, HITL gates, dialogue,
  reasoning transcripts, Cycles
- Governance: approvals, evidence packets, prompt provenance

**Load only**
- Protocol breadth: HTTP, gRPC, JMS, Kafka, Event Hubs, WebSocket, SOAP, database, browser,
  Java vuser, messaging, network
- Load generation: distributed runners, traffic profiles, runtime tuning, capacity
- Authoring: recorder, record agent, DSL, import/convert, recording profiles
- Deep analysis: `analysis` (26,942 lines), `rca` (7,914), `perfevidence` (4,277)

Nothing here overlaps. This is a genuinely complementary split: Orchestration decides *what to test
and whether it passed*; Load decides *how to generate load and what the system did*.

## Redundant — Orchestration reimplements what Load already does better

For every capability both products implement, Load's implementation is far deeper:

| Capability | Orchestration | Load | Ratio |
|---|---|---|---|
| Elastic / log search | `elk` 449 | `elastic` 7,145 | **16×** |
| Kubernetes / inventory | `k8s` 548 | `kubeops` 4,386 | **8×** |
| Object storage / artifacts | `s3` 612 | `storage` 123,613 | **200×** |
| Secrets | env-only port | `secretstore` 3,001 + `credential` 689 | — |
| APM / metrics | **none** | `dynatrace` 5,069 + `prometheus` 4,691 | Orchestration has none |
| Analysis / RCA / evidence | reporting + postmortem phases | 39,133 combined | — |
| LoadRunner control | `loadrunner` 1,400 | Load *is* the engine | — |

Two of these are strategic, not cosmetic:

1. **Orchestration drives LoadRunner directly** (1,400 lines) *and* drives Load. Two engine
   integrations for the same job, when Load is itself the engine platform.
2. **Orchestration's `env_triage` phase inspects Kubernetes and ELK itself** to judge environment
   readiness — the same systems Load already monitors through `kubeops`, `elastic`, `prometheus`
   and `dynatrace`, with APM coverage Orchestration simply does not have.

## Gaps

1. **Load cannot tell Orchestration anything.** Orchestration polls — `GET /tests/{id}`, *"one
   cheap read per poll cycle"*. Load has webhook and notification machinery referenced across 75
   files that Orchestration does not consume. Every state change costs a round trip and arrives
   late.
2. **No identity federation.** Each product authenticates separately and the handoff carries no
   authorization. An approver who decided a gate in Orchestration is anonymous to Load, and
   Orchestration's new `actor` records its own consumer id, which means nothing to Load.
3. **No shared artifact addressing.** Orchestration stores evidence in its own S3; Load stores runs
   in its own storage. A governance reviewer reconstructing a run crosses two artifact stores with
   no common identifier.
4. **Project and application identity are deliberately unlinked.** The contract states *"local and
   remote project IDs are never assumed interchangeable"*. Orchestration's run context already
   carries `Load application`, `Load release` and `Mapping revision` fields — the hook exists, but
   in this environment they all read "Not recorded".
5. **No APM in Orchestration** despite `env_triage` being an environment-readiness judgement.
6. **Dead weight:** the ADO adapter is 887 lines for a provider the README says is retired.

## Recommendations

### Consolidate — one owner per capability

**Principle: Load owns the performance-engineering telemetry; Orchestration owns the work and the
judgement.** Where both implement something, Orchestration should read it through Load when a Load
connection exists, and keep its own adapter only as the fallback for Load-less deployments.

1. **Make Load the preferred source for environment signals.** Route `env_triage`'s Kubernetes and
   log queries through Load's connection when one is configured; fall back to the local `k8s`/`elk`
   adapters otherwise. This also gives Orchestration APM coverage it has never had, for free.
   *Do not delete the local adapters* — they are the degraded path, and the connection registry
   already models enable/disable per kind.
2. **Decide the LoadRunner question explicitly.** Either Orchestration drives LoadRunner directly
   (and Load is one engine among several), or Load is the engine platform and LoadRunner is reached
   through it. Today both are true, which is why there are two engine adapters. This is a product
   decision, not a refactor.
3. **Retire the ADO adapter.** 887 lines for a provider the README already declares retired.
4. **Leave storage alone for now.** The 200× ratio is misleading — Load's `storage` is its whole
   persistence layer, not an artifact store. Unifying *addressing* (gap 3) matters; unifying
   implementations does not.

### Bridge — four concrete additions

1. **A callback from Load to Orchestration.** Load already has the notification machinery. Let
   Orchestration register a run-completion callback and drop the poll loop to a reconciliation
   safety net. This is the highest-value single change: it removes polling latency from the
   execution phase and would also make the live-stream attachment problem moot for real runs.
2. **A signed actor assertion in the handoff.** Extend the handoff contract so Orchestration can
   pass *who* is acting, signed, and Load can display and record it without trusting the URL. The
   contract's current "navigation context, not authorization" stance is right for an unsigned
   parameter; a signed assertion is the upgrade path.
3. **Populate the mapping that already exists.** `Load application`, `Load release` and
   `Mapping revision` are modelled and empty. Fill them at launch so a run carries its Load-side
   identity, then surface the reverse on Load's execution page. This closes gap 4 without
   assuming the ID spaces are interchangeable.
4. **One evidence index.** Have Orchestration's evidence packets carry the Load run identity and
   artifact references, so a governance audit resolves from one place. Orchestration already has
   the packet model; it needs the foreign reference.

### Document the deep bridge

`docs/design/APEX_UX_CONTRACT.md` was rewritten to v2 and is good, but it still describes only the
navigation handoff. Add a section for the control-plane integration: which of the ~25 operations
Orchestration may call, what it must tolerate when Load is absent or an older version, and which
capability gates are authoritative. An integration this large should not be undocumented in the
contract that governs the relationship.

### Sequence

| | Work | Why | Effort |
|---|---|---|---|
| 1 | Identifier display (follow-up 1) | Closes three open findings at one layer | ~1 day |
| 2 | Load → Orchestration callback | Removes polling latency; largest architectural win | ~3–5 days |
| 3 | Populate the Load application/release mapping | Small, and unblocks cross-product audit | ~2 days |
| 4 | Route env signals through Load when connected | Removes redundancy, adds APM coverage | ~1 week |
| 5 | Signed actor assertion + one evidence index | Completes the governance story across products | ~1 week |
| 6 | LoadRunner decision, ADO removal, contract section | Cleanup and direction | ~2 days + a decision |

## Boundaries

Sizes are line counts, a proxy for depth and not for quality. The redundancy analysis is from
source structure; I did not run Load's observability or Kubernetes paths. No Load deployment was
configured in this environment, so the handoff, the control-plane client and the callback gap are
assessed from code and contract rather than from a live integration.
