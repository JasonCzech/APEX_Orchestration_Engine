# Accessibility, review safety and shared UI remediation

2026-10-17. Implemented in **APEX Orchestration**, on `codex/holistic-ux-remediation-2026-10-17`. APEX Load was a design reference; its repository was not modified. The original findings and independent corrections remain in `ux-holistic-2026-10-17/`.

## Accessibility disposition

| Finding | Disposition and evidence |
|---|---|
| A11Y-01 | Fixed by the shared theme change: badge text uses `--accent-contrast`; shared text/fill pairs are checked across six themes. |
| A11Y-02 | Fixed: CodeMirror chrome uses code-surface tokens, rather than page text on a contrasting code background. CodeMirror CSS is imported by the shared viewers and prompt editor, including cold routes. |
| A11Y-03 | Fixed jointly with the correctness work: chart lines and legends use matching dash patterns; bar data uses patterns/labels and accessible numerical alternatives. Colour is no longer the only series identifier. See the correctness disposition. |
| A11Y-04 | Fixed: editable prompt CodeMirror sets `indentWithTab={false}`. A real browser verifies Tab leaves the editor without changing prompt text or arming Save. |
| A11Y-05 | The independent review downgraded this to P3, not a demonstrated lost-keystroke failure. Selection now updates a synchronous requested-selection ref; burst navigation regression tests preserve every step. |
| A11Y-06 | Fixed: the mounted run workspace owns one polite, atomic announcement of review, phase and live-transport transitions. It does not rely on the unused RunRail component. |
| A11Y-07 | Fixed by the shared phase display model: visible and accessible phase names/statuses use the same human-readable labels. |
| A11Y-08 | Fixed in the wizard: deferred focus checks whether the user already moved. A cross-cutting follow-up fixes the shell's deferred route focus too: destination controls and menus retain focus, while persistent navigation controls still transfer focus into the new content. |
| A11Y-09 | The original roving-tabindex defect was disproved during verification. The actual focus gap is fixed: j/k selection focuses the selected option, announces its position and review kind, and removes the redundant unfocusable-parent `aria-activedescendant`. |
| A11Y-10 | Fixed across Home, Settings, wizard sections and resource panels: section headings below the shell h1 are h2. Existing nested subsections retain their hierarchy. |
| A11Y-11 | Fixed: the CodeMirror editable/read-only content DOM receives its specific accessible name through `EditorView.contentAttributes`, not only the wrapper. |
| A11Y-12 | Fixed: CodeMirror content itself is keyboard-focusable, including read-only viewers, and remains scrollable; no accessibility rule is suppressed. |
| A11Y-13 | Fixed: decision consequences are a named section, not a complementary `<aside>` nested inside the review workspace. |
| A11Y-14 | Fixed by the shared theme change: each palette declares its colour scheme and form surfaces. Cold-load browser checks cover all six palettes. |
| A11Y-15 | Fixed beyond the spacing exception: shared tabs are at least 36px high, menus at least 32px, and formerly small level filters/run-title controls at least 28px. Browser checks at 390px verify direct targets at least 24px. |
| A11Y-16 | Fixed: disabled buttons, fields, toggles and wizard retry controls use explicit readable disabled text/background tokens at opacity 1. Six-theme contrast checks include disabled surfaces. |
| A11Y-17 | Fixed latent calendar tokens: cron and ad-hoc text use readable colours, including Apex Light and Solarized Dark. The contrast checker resolves and alpha-composites their sRGB colour mixes, checking all six palettes even where calendar data is unavailable. |

## Agent and review experience disposition

| Finding | Disposition and evidence |
|---|---|
| AGT-01 | Fixed jointly with the backend: output-review payloads carry a bounded public phase-evidence projection. The decision point shows available provenance, reasoning summary, tools and transcript links, and says when evidence is absent. |
| AGT-02 | Fixed: Agent reasoning links the durable transcript and download; a missing digest no longer hides a collected transcript. |
| AGT-03 | Fixed: prompt source and retained editor metadata are shown at the review; application-wide edit scope is explicit. Backend authenticated actor recording is covered in the governance disposition. |
| AGT-04 | Fixed by shared badge semantics: prompt and output review kinds have distinct token styling and explicit text labels. |
| AGT-05 | Fixed by the common status normalizer: unknown data is Unknown, not Pending. |
| AGT-06 | Fixed: tool outcomes use a neutral fallback; running, timeout and denied are never promoted to success. |
| AGT-07 | Fixed by phase-strip wiring: completed and active connectors are emitted as real classes. See correctness checks. |
| AGT-08 | Fixed: the phase workspace uses the same normalizer with thread status, phase strip and pending-review context as the strip and header. |
| AGT-09 | Fixed: digest copy has an explicit “Agent reasoning summary” label. |
| AGT-10 | Fixed: the prompt-version reference wraps, and the tooltip preserves the complete reference rather than replacing it with an explanation. |
| AGT-11 | Fixed: waiting age uses elapsed-duration wording; it no longer concatenates “Waiting” and a relative “ago” timestamp. |
| AGT-12 | Fixed in Home: its phase-health caption explicitly names succeeded/failed as the counted subset and excludes skipped, aborted, waiting and active phases. No unavailable aggregate is invented. |
| AGT-13 | Fixed: Skip phase requires confirmation and names its irreversible scope and downstream dependency consequences. Dirty prompt text is explicitly excluded from the skip action. |
| AGT-14 | Fixed: retained JSON transcript envelopes render messages as readable multiline text; the raw download remains available. |
| AGT-15 | Fixed in the runs grid: compact phase strips are rendered and parity documentation reflects actual behaviour. |
| AGT-16 | Fixed with A11Y-07: the phase strip exposes the same vocabulary it displays. |
| G-01 / G-02 | Fixed: skip confirmation plus a priority navigation guard for dirty prompt, discuss and revision drafts. QueryClient-owned buffers are keyed by interrupt ID and restored after leaving and returning. |
| G-03 | Fixed: accepted local modify/revise/discuss waits for that run's new review instead of reporting completion elsewhere. Independent review found and fixed two cross-cache races: a stale inbox must not settle a local action, and switching runs must not leave a phantom “Agent working” row. Captured thread/inbox generations guard both paths. |
| A-01 / A-02 | Fixed: actual option focus and queue announcements; after a completed decision, selection advances to the next open row rather than resetting to the top. |
| R-01 / R-02 / S-02 | Fixed jointly: terminal phase copy describes recorded absence, live chips are suppressed on terminal runs, elapsed time explicitly includes reviews, and stream transitions are announced. |
| ARE-24 / ARE-25 | Fixed: prompt diff colours use code-compatible text and backgrounds with added/removed cues; PromptDiff owns its CSS on cold Cycles routes. Creating a prompt explicitly discloses that it immediately becomes active; archive uses confirmation. |

## Consistency disposition

| Finding | Disposition |
|---|---|
| CON-01 | Shared tonal notices live in entry-loaded theme primitives, including success; cold-route browser checks no longer depend on visiting run detail. |
| CON-02 | All formerly noncompliant tab sets use `TabList`. `TabPanels` renders a real target for **every** tab, hidden when inactive, while mounting content only for the active panel to preserve prior lifecycles. Context/Evidence/Engine/wizard already retain real panels. Arrow/Home/End, one focus stop, disabled writes and every `aria-controls` target are tested. |
| CON-03 / CON-15 | Review is the human-facing pause workflow; Decision remains the recorded outcome. Agent log replaces Pipeline log; run IDs replace Thread labels; prompt/action labels use sentence case. Shell, catalog and ticket vocabulary are covered in the governance disposition. |
| CON-04 | Unknown routes use an explicit not-found StateCard with recovery; the old global empty-state override is scoped. Verification's downgrade is preserved: the original claimed computed-style change was not reproduced. |
| CON-05 | Shared StateCard separates empty, filtered, unauthorized, not-found, unconfigured and stale states with an explicit next action where one exists; ProblemCard remains the error primitive. Shared Skeleton separates loading from absence. Adopted across admin, prompts, review inbox, run workspaces, artifacts, tickets, cycles and the other agents' Home/compare/analytics/logs/resource surfaces. Decorative skeleton rows do not create extra announcements. |
| CON-06 | All dialogs retain the existing inert/focus stack and use the shared backdrop token. Dirty-review confirmation owns cold-route styling instead of depending on the prompt catalog stylesheet. |
| CON-07 | Skip and prompt archive use explicit confirmation; dirty navigation uses the existing priority guard. Cycles review confirmations are covered in its separate change. |
| CON-08 / C-01 | Shared timestamp formatting includes year and timezone; the local/UTC preference is covered in the correctness work. Byte formatting is shared by artifact/context/wizard paths, including invalid-size handling. |
| CON-09 / CON-10 | Shared theme corrections define consumed tokens, apply theme/motion preferences before the first content frame and update the documented palette inventory. See the shared theme browser checks. |
| CON-11 | Shared pagination now includes the provider script library and Cycles. Provider pages use the returned total and backend limits (100 rows, offset 5000); cycles use page length and offset 10000 because the API does not return a total. Both have local page sizes, reset offset on size changes, and do not mutate the Tests default. Other filters/sorts/pagers are covered in the correctness/resource changes. |
| CON-12 / CON-13 | Phase KPI rates use the shared percentage formatter; table labels and comparative precision use the correctness changes. |
| CON-14 | Redundant page titles were removed from admin and the APEX Load workspace; section headings retain a coherent hierarchy. Other shell/resource fixes are covered by their owners. |
| CON-16 | Prompt provenance remains readable in full; IDs elsewhere use the shared copyable identity primitive in the resource/correctness changes. |
| CON-17 | Removed the duplicate Sidebar pulse definition, redundant local reduced-motion overrides and migrated local skeleton rules/animations. Shared byte, timestamp, status and action primitives replace the relevant copies. |

## Verification and review cycles

- First review-safety item: independent review/fix cycles cleared cold-dialog styling, redundant focus semantics, retained-row settlement and stale-generation races before commit `fa811e3`.
- Shared state primitives: independent review clear, seven unit tests and ESLint passed before `f9d0889`.
- Bulk state/tab review found missing inactive panel targets; fixed through TabPanels and regression tests, then independently re-reviewed clear.
- Cross-cutting focus review found persistent drawer-trigger focus; excluded persistent topbar controls without stealing newly focused destination/menu controls. Both regressions pass.
- Contrast review found transparent colour mixes could divide by zero; fixed with a dedicated test. Six contrast unit tests pass; 750 palette/surface pairs pass at 4.5:1 or above.
- The affected 319-test set passed after four stale human-label assertions were corrected (315 initial passes, 31/31 focused rerun including all four failures). Earlier state/admin/form targeted checks also passed; final combined-suite results are recorded by the lead.
- Production build succeeded after integrating current `origin/main`. Production Playwright passed **12/12**: five cold-route tab workflows, mobile targets/disabled opacity, filtered/not-found recovery, two real-CodeMirror review workflows, and three resource workflows across viewer/operator/admin.

This is regression verification of the reported defects, not a WCAG conformance certification. Browser checks exercise DOM focus and live-region semantics; an actual assistive-technology user session, real external provider execution and production auth expiry remain separate validation contexts. No raw reasoning is fabricated: the UI shows only retained public evidence.
