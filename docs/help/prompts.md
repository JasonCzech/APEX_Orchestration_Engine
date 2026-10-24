# Prompts & pinned versions

Use [Prompts](/prompts) to inspect reusable instructions. In [New test → Prompts](/runs/new?step=prompts), choose the instructions for the test. Catalog reads respect audience scope. Creating alternatives, activating catalog versions, or replacing defaults requires an unscoped administrator; a run-specific edit does not grant catalog-editing permission.

## Understand the four instruction levels

- **System Prompt**: exactly one shared selection for the agent pipeline.
- **Phase context**: one selection for each agent-owned phase.
- **Application context**: an ordered optional list of applicable application/shared instructions.
- **Environment Context**: an ordered optional list associated with environments in scope.

All Namespaces aggregates the catalog. Compatibility entries describe historical configurations. Catalog names are identifiers; applicability comes from explicit metadata rather than a naming convention.

## Select and inspect versions

1. Open each relevant prompt category in the wizard.
2. Inspect the selected name and immutable version. Required selections initialize from available defaults; optional lists start empty.
3. Add applicable context in the order it should be read. Remove incompatible selections after a scope change.
4. Where offered, edit the test's override content. This changes the current setup rather than the shared catalog.
5. Review selected versions and outstanding upgrade issues before launch.

Changing the active catalog version does not silently update an existing draft or run. Use the explicit update action for a draft. At launch, the server freezes authorized selections and effective content into a prompt manifest. Resume and rerun use that recorded manifest; later catalog edits do not rewrite it.

## Revise instructions during a review

For current manifest-based tests, the shared System Prompt is read-only during phase review. You may revise supported phase and optional context content for the current phase, while selected identities, list membership, and order remain fixed. Use the displayed save/re-review action before executing revised work.

Review-time revisions do not publish a catalog version or change instructions for later phases. Historical tests can expose their older review contract. Inspect the actual available controls rather than assuming every historical run behaves like a newly authored one.

## Templates and output requirements

Pipeline templates support simple `{title}` and `{request}` substitutions. Use `{{name}}` for a literal brace-wrapped identifier. JSON examples do not need escaping. Formatting modifiers and variable traversal are unsupported; unknown variables fail resolution.

The template preview can experiment with sample variables, but those do not become runtime variables. Rendered review text is not interpolated a second time.

The platform supplies a fixed policy and structured deliverable contract in addition to your prompts. An agent must provide the required phase sections, evidence, assumptions, gaps, and blockers. A complete plan needs a valid workload distribution. Prompt text cannot bypass validation, select unauthorized workloads, or create live target approval.

## Maintain the shared catalog

An authorized administrator creates alternatives, adds immutable versions, activates the chosen version, and explicitly replaces defaults when needed. A current default must have a replacement before archival. Review applicability and template rendering before activation.

For rollback, select a prior valid version through supported activation controls and review affected future configurations. Existing launched runs retain their frozen instructions. For execution-specific constraints, see [Capabilities & limitations](capabilities.md).
