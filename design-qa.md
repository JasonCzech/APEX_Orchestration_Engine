# Evidence redesign — design QA

- Source visual: `/Users/jasonczech/.codex/generated_images/01a0a185-8ac3-7ac3-a112-2f99a5120f65/exec-540145f9-5ab4-4323-b711-01c8d68b63de.png`
- Implementation: `http://127.0.0.1:3000/evidence`
- Browser evidence: Codex in-app browser captures at the default desktop viewport and a compact 666 CSS-pixel viewport. The final desktop capture and source visual were emitted together in one comparison input.
- State compared: dummy-data Evidence index, first linked item selected, Provenance tab open.

## Comparison

The implementation preserves the selected concept's primary hierarchy and interaction model: a persistent project/application/test/source/search filter bar, project → application → test scope rail, dense evidence result list, selected-row treatment, and a dedicated details pane with Content, Details, and Provenance tabs.

Intentional data-contract adaptations:

- The current `EvidencePacket` API exposes id, source, title, summary, reference, and thread id. The implementation displays all of these and joins the thread id to the pipelines catalog to expose real project, application, test, status, and update time.
- Mock-only file metadata such as byte size, checksum, phase, host, collector, pagination, and downloadable file content was not fabricated.
- Unassociated packets are explicitly labeled Project-wide / unlinked and show Not associated or Not recorded instead of implying a relationship.

## Defect audit

- P0: none.
- P1: none.
- P2: none after the hierarchy/count correction and responsive pass.
- P3: the source concept includes richer artifact-store fields and sort/pagination controls that cannot be supported by the current evidence endpoint without a contract extension.

## Verification

- Desktop visual comparison: passed.
- Compact responsive layout: passed.
- Search, clear, row selection, URL filter persistence, detail tabs, and run deep link: passed.
- Browser console warnings/errors: none.
- Evidence feature tests: 19 passed.
- Typecheck, lint, and production build: passed.
- Full dashboard suite: 912 passed; one unrelated router smoke failure remains on `/approvals` waiting for the `Needs review` heading.

final result: passed
