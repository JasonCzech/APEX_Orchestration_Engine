# Audit notes

- Test History pending-gate action opens `/runs?selected=run-active&runSection=workspace&phase=execution` and preserves the master-detail history context.
- Approvals uses the same gate engine with a compact presentation that omits run result, progress, pipeline, phase summary, evidence coverage, logs, and reasoning.
- Approvals selection does not update the URL.
- `Open run` from Approvals links to `/runs/run-active`, which opens Overview rather than the pending gate.
- Overview requires another `Review pending decision` action to reach the full workspace.
- Queue triage, oldest-first ordering, wait age, and keyboard navigation are useful and should survive consolidation.
- No decision action was submitted during the audit.
