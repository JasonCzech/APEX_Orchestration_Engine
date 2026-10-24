# Run, review & complete phases

Open [Tests](/runs), choose a run, and use its phase controls, Review, Timeline, and artifacts to follow work. You need read access to the complete run scope. Review decisions, human completion, reruns, and aborts additionally require operator capability in that scope.

## Understand the seven phases

1. **Story Analysis** establishes objectives, requirements, gaps, and acceptance context.
2. **Test Planning** develops scenarios and workload distribution.
3. **Env Triage** assesses the environment and readiness evidence.
4. **Script & Scenario** prepares or references the workload and its validation evidence.
5. **Execution** uses the approved engine/workload path or waits for human-supplied execution evidence.
6. **Reporting & Analysis** collects selected analysis results and assembles a report.
7. **Postmortem** records findings, follow-up actions, and remaining uncertainty.

The run shows phase state and ownership. An agent-owned phase can pause at prompt/output reviews according to policy. A person-owned phase waits for completion of its human task. Historical runs can retain older phase subsets and skipped states.

## Record a review decision

Open a pending review from Home, Tests, or the run workspace. Verify the run and phase before acting. At a prompt review, read the effective instructions and any source context. For supported edits, save/re-review before starting execution.

At an output review, inspect the structured deliverable, referenced artifacts, assumptions, gaps, and blockers. Review evidence beyond the summary when a decision depends on exact measurements. Use **Accept phase output & continue** only when the work meets its requirements. A blocked deliverable cannot be accepted as successful.

Use **Discuss with the agent** to ask a question. Discussion records an exchange and leaves the review pending. To change the work, choose **Request changes**, enter concrete revision instructions, and use **Re-run with changes**. Inspect the revised output before accepting it.

Decisions are bound to the current interrupt and recorded with authenticated actor identity. A receipt confirms the recorded decision. If another operator acts first, a superseded message means the old review is no longer actionable; reopen the current run state. A network failure preserves the same-review draft for retry. Do not replay an old decision against a new review.

This prompt-review example uses bundled offline demo data. The shared System prompt is read-only; the phase's execution action appears below the inspected instructions:

![Offline demo Test Planning prompt review with instruction tabs and Execute phase control](/help/images/prompt-review.png)

## Complete human work

Human ownership requires structured output. The task identifies phase, attempt/revision, required sections, upstream artifacts, engine, workload, and applicable tool requirements. Complete the form for that exact task. A short free-text note is insufficient where structured evidence is required.

- Every phase needs its required summary, sections, evidence, and complete disposition.
- Human Test Planning needs valid workload scenarios and percentages.
- Human Script & Scenario feeding agent-owned execution needs a valid load-test specification. It cannot override an approved provider binding by naming an arbitrary script.
- Human Execution needs engine-compatible external results, a source URI, and nonempty measured KPIs. Internal RCA additionally requires a verified native APEX Load run identity. Provider-derived measurements are authoritative when retrieved.
- Human Reporting & Analysis needs references for every selected analysis connection. These identify human-supplied results; they do not establish that the connectors ran automatically.

Submit the task and wait for validation. A received submission is not yet completed work. If validation fails, correct the exact fields and resubmit the same current task. Drafts remain available for same-task errors; a new task revision or evidence contract resets the old form. A completion records executor, actor, and time.

## Follow progress and evidence

The phase workspace is more current than periodically refreshed fleet summaries. A stream reconnection does not imply a new execution. The durable run state is the source of truth after refresh.

Pending output-review evidence can appear in summary, artifacts, and Timeline before acceptance. Inspect it as pending work. Earlier accepted output and a new unaccepted revision are different evidence states. See [Results](results.md).

## Rerun or abort

Use the supported rerun control to repeat the selected phase window on the existing run. The original ownership plan and frozen prompt manifest are retained. A copied setup followed by launch creates a new test/run instead.

Abort requires an explicit confirmation, using the keyword shown by the UI. Graph cancellation and engine-level stop are separate operations; remote work or cleanup can need follow-up. Read the resulting state rather than assuming an abort button proves every remote resource stopped. [Diagnose & recover](troubleshooting.md#provider-and-analysis-recovery) covers paused provider operations.
