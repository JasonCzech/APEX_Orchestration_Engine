# Drafts, presets & launch

Drafts preserve work in progress. Presets supply reusable starting configuration. A launched run records the configuration actually accepted by the server. Operators can save and reuse authorized setup; particular preset-editing operations can require global administration.

## Save and resume a draft

Start from [New test](/runs/new), a saved draft on Home, or a supported copy-setup action on an existing run. Watch the draft status: **Saving…**, **Draft saved**, or **Draft save failed**. An incomplete draft can be saved, but launch still requires a valid plan.

If saving fails, preserve your work and resolve the displayed problem. Browser/session changes can affect local recovery state. Do not assume another origin or browser has the same preferences, drafts, or key. Navigation guards warn about unsaved edits; read the warning before leaving a review or form.

## Apply and save presets

Use [Test presets](/test-presets) to inspect available setup. In the wizard, choose a preset through the start controls or preset bar, then inspect the resulting fields and changes. A preset can affect engine, prompts, scope, ownership, or review policy depending on its contents.

Scope-neutral presets preserve the current topology. Structured scope can replace it, and a preset without traffic entry can clear a previous execution environment. Older scalar scope requires explicit completion. Changing the owning project can invalidate project-bound sources. Review every reconciliation or migration issue before proceeding.

Where available, **Save preset** records reusable setup with an appropriate name and audience. Do not save credentials in descriptive fields. A preset is not execution authority and does not replace current target/provider checks.

## Launch readiness

1. In **Review**, confirm title/request, every scope participant, primary ownership, and traffic entry.
2. Check engine, approved target, provider workload/release mapping, requested workload, and acceptance thresholds.
3. Inspect all seven owners, review policy/counts, selected prompt versions, source tickets/documents, and analysis connections.
4. Follow outstanding planning or execution items to their fields. Use retry controls for failed verification.
5. Choose **Launch test** and wait for the created run's identity and destination.

Configuration readiness checks metadata and required fields. Provider connectivity, credentials, capacity, and exact workload eligibility can fail during later operations. The server rechecks target approval/version before live dispatch. See [Configure workload & execution](configuration.md).

## Recover an uncertain launch

The dashboard retains an idempotent launch attempt so an unchanged retry can recover its existing result. A changed intent can be a different operation. Do not edit the request merely to escape an uncertain attempt, and do not repeatedly create duplicate tests.

After a lost response, inspect the dashboard's message, Tests, and the durable attempt state. Reuse a supported retry for the same intent. If the UI quarantines an older/uncertain request, resolve it through the displayed recovery guidance or administrator investigation rather than recreating it blindly.

## Reuse historical configurations

Copied setup is revalidated against current catalogs and permissions. Older selected-phase subsets are interpreted as agent ownership for included phases and human ownership for the rest; the wizard shows a human-completion notice. Historical runs keep their original execution semantics.

Check scope, prompt upgrade issues, unsupported engines, and incompatible analysis selections after copying. A new launch is distinct from a phase rerun on the original run. [The phase guide](phases.md#rerun-or-abort) explains that difference.
