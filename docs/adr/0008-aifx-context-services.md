# ADR-0008: AIFx Context Services from GitHub Enterprise, pinned at launch

**Status:** accepted (2026-11-04)

## Context
AIFx Context Services are GitHub Enterprise repositories that hold the agent rules,
memories, skills and application context the phase agents should follow:

- `global-rules` per organization and `<apm>-team-documents` per APM, which
  together define a team's platform policy (system context);
- the ACB (Application Context Builder), one repository per application, owned by
  the application development team and the source of truth for its architecture.

Environment and Performance Phase Context Builders (ECB, PCB) will follow.

## Decision
1. **Source control is a port.** `SourceControlPort` gains `resolve_commit`; the
   `github_enterprise` adapter authenticates as a GitHub App (RS256 app JWT →
   installation → short-lived `contents:read` token) and reads files at a commit
   SHA. The `stub` provider serves deterministic AIFx repositories so dev, CI and
   demos run offline (ADR-0002).
2. **Admins map, conventions suggest.** `aifx_organizations` names a *global*
   source-control connection, the GitHub owner, the global-rules repository and
   naming conventions. `aifx_application_sources` gives an application its APM ID,
   team-documents and ACB repository. Conventions pre-fill; a project-scoped
   admin is held to them, and only an unscoped platform admin can map another
   repository (the GitHub App can read every repository it is installed on).
3. **Repositories declare their content** in `aifx.yaml` (`schema: aifx/v1`, a
   role, and entries of type `rule`, `memory`, `skill` or `context` with phases).
   Nothing undeclared is read.
4. **Pin at launch, not via the prompt catalog.** Launch resolves each source to
   a commit, reads the manifest and entries at that SHA, and freezes them into the
   server-owned `configurable["aifx_snapshot"]` (`AifxSnapshotV1`, self-verifying
   with per-entry and snapshot SHA-256). Like `prompt_manifest`, clients can never
   supply it; reruns inherit it; resumes never read GitHub again.
5. **Precedence.** The code-owned platform policy and output contract stay first
   and are not edited. A fixed AIFx preamble follows, then global rules, team
   rules and skills, then the catalog's shared system prompt and contexts, then
   ACB, then the phase context. Rules and context are instructions; skills are
   listed inline (a tool-loaded skill would be "untrusted evidence" under the
   platform policy); memories are fenced evidence in the user message.
6. **Visible and auditable.** The prompt-review gate shows the pinned sources at
   their commits and this phase's entry titles; `GET /v1/pipelines/{id}/aifx`
   serves the content read-only. Each phase result and transcript records
   `aifx_snapshot_sha256` and the entry IDs; `model_input_sha256` covers the text.

## Rationale
Pinning at a commit gives every run a reproducible input without a catalog
import or approval workflow, and keeps a gated run stable while its repositories
move. Reading through the existing port, connection resolver, credential scanner
and budgets reuses the controls every other integration already passes.

## Consequences
- **Repository write access steers agents.** Protect AIFx repositories with
  branch protection and CODEOWNERS, and prefer a promoted tag as the ref.
- **Credential-shaped prose blocks a launch.** The shared scanner is not
  weakened for AIFx; the admin preview names the file and line to rephrase.
- **Locked environments fail closed** when a mapped source cannot be read; dev
  environments launch with a `degraded` snapshot that lists the problems.
- **Budgets.** `APEX_AIFX__*` caps manifests, files, sources, the snapshot and
  each phase's share of the model input; launch fails rather than truncating.
- **Not covered:** runs in one cycle can pin different commits; Cycle dispatch
  and APEX Load native report runs do not carry AIFx context.
