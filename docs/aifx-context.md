# AIFx Context Services

AIFx Context Services feed GitHub Enterprise content to the pipeline's phase
agents. This page covers deployment, onboarding and repository authoring; the
design and its trade-offs are in [ADR-0008](adr/0008-aifx-context-services.md).

## Deploy

1. Create a GitHub App on GitHub Enterprise with **read-only** repository
   permissions for *Contents* and *Metadata*, and install it on every AIFx
   repository (or the whole AIFx organization).
2. Store its private key where the API can read it as an
   `APEX_INTEGRATION_*` environment variable (on AKS, from Key Vault via the CSI
   driver). A one-line value with literal `\n` escapes is accepted.
3. Set `APEX_AIFX_ENABLED=true`.
4. Create a **global** source-control connection, provider `github_enterprise`:

   ```json
   {"kind": "source_control", "provider": "github_enterprise", "name": "ghe-aifx",
    "base_url": "https://github.example.com/api/v3",
    "options": {"base_url": "https://github.example.com/api/v3", "app_id": "12345"},
    "secret_ref": "env:APEX_INTEGRATION_GHE_APP_PRIVATE_KEY"}
   ```

   An internal GHE host on a private network needs the platform-admin
   `_apex_trusted_private_host` option (or bootstrap approval in locked
   environments). Corporate certificate authorities come from `SSL_CERT_FILE`.
   **Test connection** authenticates as the app (`GET /app`).

### Limits (`APEX_AIFX__*`)

| Setting | Default | Meaning |
|---|---|---|
| `MAX_MANIFEST_BYTES` | 32768 | `aifx.yaml` size |
| `MAX_FILE_CHARS` | 50000 | one entry's file |
| `MAX_ENTRIES_PER_SOURCE` | 50 | entries in one manifest |
| `MAX_SOURCES` | 8 | repositories one test reads |
| `MAX_SNAPSHOT_CHARS` | 250000 | all content pinned by one test |
| `MAX_PHASE_SYSTEM_CHARS` | 60000 | one phase's rules, skills and context |
| `MAX_PHASE_MEMORY_CHARS` | 30000 | one phase's memories |
| `FETCH_TIMEOUT_S` | 30 | the whole read at launch |
| `FETCH_CONCURRENCY` | 4 | parallel file reads per repository |
| `REQUIRED_IN_LOCKED` | true | locked environments refuse partial context |

## Onboard

On **Admin → AIFx context** (`/admin/aifx`, API `/v1/admin/aifx`):

- A platform admin onboards an **organization**: the connection, GitHub owner,
  global-rules repository (default `global-rules`), naming conventions
  (`{apm}-team-documents`, `{apm}-acb`) and default ref.
- An admin onboards each **application**: APM ID, then **Suggest repositories**
  fills team documents and ACB from the conventions. A project-scoped admin can
  only save (or validate) the convention names, and needs access through every
  project that uses the application; only an unscoped platform admin can map a
  repository that does not follow the convention.
- **Validate** (`POST …/applications/{id}/preview`) is a dry run of what launch
  will pin, including the source-count, snapshot and per-phase budget checks.
  It returns each source's commit and entries, and every problem by
  repository, path and line, never file content.
- A connection that an organization uses cannot be deleted or moved into a
  project (409).

## What a test reads

| Source | From | Role in the prompt |
|---|---|---|
| Global rules | the primary system's organization | instructions after the platform policy |
| Team documents | the primary system's APM | instructions after global rules |
| ACB | every onboarded participating application | instructions after the catalog's application contexts |

If the primary system is not onboarded, global rules come from the first
onboarded participant's organization and no team documents are read.

## Author a repository

Every AIFx repository has an `aifx.yaml` at its root:

```yaml
schema: aifx/v1
role: application-context      # global-rules | team-documents | application-context
entries:
  - id: architecture           # lowercase letters, digits, '-'; unique here
    type: context              # rule | memory | skill | context
    path: context/architecture.md
    phases: all                # or a list of phases
    title: Application architecture
  - id: known-limits
    type: memory
    path: memories/known-limits.md
    phases: [test_planning, env_triage, reporting]
    title: Known capacity limits
```

- Phases: `story_analysis`, `test_planning`, `env_triage`, `script_scenario`,
  `execution`, `reporting`, `postmortem`. Execution uses no model, so entries
  only reach agent phases.
- `rule` and `context` become instructions; `skill` entries are listed as
  techniques (a `description` is required); `memory` entries are history and
  reach the agent as fenced evidence, never as instructions.
- Only declared `.md`, `.markdown` and `.txt` files are read. Paths are relative,
  with no `..`, symlinks or submodules. YAML aliases and duplicate keys are rejected.
- Write prose, not credentials. The platform rejects anything shaped like a
  secret, including lines such as `token: …`, `password = …` or
  `Authorization: Bearer …`. Write "authentication uses OAuth2 via Okta" instead.
- Every launch pins a commit. Review changes through pull requests, and prefer
  pinning organizations to a release tag rather than a moving branch.

## At run time

- The prompt-review gate lists the pinned repositories with their commits and
  the entries this phase follows, with the content read-only on request.
- `GET /v1/pipelines/{thread_id}/aifx` returns the run's verified snapshot.
- Each phase result and transcript records `aifx_snapshot_sha256` and the
  entry IDs its agent ran with.
- Resumes and reruns never read GitHub again. Clients cannot send
  `aifx_snapshot`; a native LangGraph resume that omits it runs with the
  snapshot from the run's checkpoint.
