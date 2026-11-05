# AIFx Context Services

Open [AIFx context](/admin/aifx) with an admin session. AIFx Context Services connect the pipeline to GitHub Enterprise repositories that hold agent rules, memories, skills and application context. Every test pins the repositories it reads at a commit when it launches, so a gated run, a resume, or a rerun always uses exactly the content it started with.

The page appears only when the deployment enables AIFx (`APEX_AIFX_ENABLED`).

## What each repository contributes

- **Global rules** (one per organization, conventionally `global-rules`) and **team documents** (per APM, conventionally `<apm>-team-documents`) together form the platform policy for that team's tests. They rank below the built-in platform policy and output contract, and above the selected catalog prompts.
- **ACB** (Application Context Builder, conventionally `<apm>-acb`) is the application development team's repository and the authoritative description of the application's architecture. Every onboarded application in a test contributes its ACB.
- Rules and context become instructions. Skills are listed for the phases they apply to. Memories are history, so agents receive them as evidence, never as instructions.

## Onboard an organization

A platform admin (unscoped) onboards organizations.

1. Create a global source-control connection with the **GitHub Enterprise (GitHub App)** provider on [Connections](/admin/connections). Enter the API URL (ending in `/api/v3`), the GitHub App ID, and an `env:APEX_INTEGRATION_*` reference to the app's private key. **Test connection** authenticates as the app.
2. On AIFx context, choose **Onboard organization**, select that connection, and enter the GitHub owner that holds the AIFx repositories.
3. Confirm the global rules repository, the naming conventions (`{apm}` is replaced by each application's APM ID), and the default ref. A protected branch or release tag is safest, because whoever can push to these repositories changes what the agents are told.

The GitHub App needs read-only **Contents** and **Metadata** permissions and must be installed on every AIFx repository.

## Onboard an application

1. Choose **Onboard application**, select the application and organization, and enter its APM ID.
2. Select **Suggest repositories** to fill the team documents and ACB names from the organization's conventions. A project-scoped admin must keep these names; ask a platform admin to map a repository that does not follow the convention.
3. Select **Validate**. The dry run resolves each repository to a commit, checks its `aifx.yaml`, lists every declared entry with its size, and applies the same size limits a launch does. Problems name the repository, file and line. Change the form and the result is marked out of date until you validate again.
4. Save the mapping. Tests launched afterwards for this application pin its AIFx context.

## The `aifx.yaml` manifest

Each repository declares what to read in an `aifx.yaml` at its root:

```yaml
schema: aifx/v1
role: global-rules        # or team-documents, application-context
entries:
  - id: evidence-first
    type: rule            # rule, memory, skill or context
    path: rules/evidence-first.md
    phases: all           # or a list such as [test_planning, reporting]
    title: Evidence-first performance engineering
  - id: workload-modeling
    type: skill
    path: skills/workload-modeling.md
    phases: [test_planning]
    title: Workload modeling
    description: Derive arrival rates from production traffic evidence.
```

Only declared `.md`, `.markdown` and `.txt` files are read; other files in the repository are ignored. Skills need a description.

## Fix validation problems

- **Text looks like a credential**: the same scanner that guards every model input rejects lines shaped like a secret, such as `token: …` or `password = …`, even in prose. Rephrase the line the problem names (for example "authentication uses OAuth2 via Okta").
- **Not available to the GitHub App**: the repository, ref or file does not exist, or the app is not installed on it.
- **Role mismatch**: the manifest's `role` must match how the repository is mapped.
- **Size limits**: a file, a manifest, or one phase's share of AIFx content exceeds the deployment's limits. Split or trim the content.

## At prompt review

The prompt-review sidebar lists the pinned repositories with their commits and the entries this phase follows. **Show pinned content** displays them read-only. AIFx content is changed in its repository, never at a gate. A run's phase details record the snapshot and entries its agent ran with.
