# Results, evidence & comparison

Open an authorized run from [Tests](/runs). Use its phase results, artifacts, and Timeline, or browse [Evidence](/evidence). Evidence availability, orchestration progress, and acceptance outcome describe different facts.

## Interpret an outcome

An engine can complete its lifecycle while the measured result fails an acceptance threshold. An orchestration phase can be pending human review after the remote engine has finished. A completed phase can carry gaps that limit the conclusions you can draw.

Check the requested workload, actual measurements, source engine/run identity, assessment completeness, threshold outcome, and evidence coverage together. Unknown or uncollected metrics must remain unknown. A measured zero is valid data; it is not a substitute for an absent measurement.

Simulation and offline demo measurements are illustrative. A model summary is an interpretation of evidence, not an independent measurement. A successful schema check establishes format, not the truth of a citation or causal explanation.

## Inspect artifacts and transcripts

1. Open the relevant phase and read its summary, disposition, assumptions, gaps, and blockers.
2. Follow the artifact links to inspect full structured output or transcripts. The artifact viewer supports available JSON/text previews and downloads; large or binary content may require download.
3. Verify the artifact identity and phase/attempt. A transcript can expose effective instruction provenance and the work behind the summary.
4. Return using the viewer's Back to phase, Back to review, or Back to run link so you retain context.

Artifact access goes through authorized retrieval. An artifact's existence does not imply it has been accepted. The current pending review may expose evidence that remains unapproved. Missing files, denied access, or storage errors must be resolved before relying on a conclusion that needs them.

## Read the timeline

Timeline organizes recorded work and decisions. Inspect phase transitions, review activity, retries, and evidence references. Actor attribution comes from recorded authenticated activity; unknown identities must remain unknown.

Do not infer an exact time for evidence where none was recorded. A reconnect or refresh can reconcile the display without creating a new event or execution. Keep the run identity and relevant timestamps when investigating a mismatch.

## Compare runs

Use the Tests comparison controls or [Compare runs](/runs/compare) to select accessible runs. Check whether scope, environments, workload, engine, and evidence coverage are comparable before interpreting differences.

Only collected measurements can support numeric ranking or baseline conclusions. An absent metric cannot be treated as zero, and an unassessed outcome cannot be treated as a pass. Descriptive comparisons identify differences; they do not establish causality or authorize baseline promotion.

Cycle result comparisons use frozen report evidence and its coverage rules. An accepted report may become stale when its dependencies change. See [Manage test cycles](cycles.md#review-reports-and-follow-up).

## Resolve incomplete evidence

For a remote job that succeeded but whose result has not been collected, use the current recovery workflow to collect the existing job. Do not submit another analysis solely because the local artifact is missing. [Reporting & analysis tools](analysis.md#recover-analysis) explains retained job identities and attempts.

If results contradict source measurements, retain both identities, observation times, and the exact discrepancy. Ask for a revision or escalate with the full artifact identity. Prefer explicit uncertainty to accepting a polished summary unsupported by evidence.
