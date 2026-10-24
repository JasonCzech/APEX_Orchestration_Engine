# Applications & environments

[Applications & environments](/environments) supplies the catalog used by scope selection and live target checks. An application can participate in authorized projects and contain environments. An environment supplies relevant infrastructure context and, where approved, a live traffic destination.

Read access follows catalog audience. Mutations require operator-or-higher capability for that audience; platform-wide records can require global administration. Your scope must cover each participant selected for a test.

## Maintain useful catalog records

1. Open Applications & environments and select the intended application/environment.
2. Inspect project associations, names, descriptions, environment kind, and inventory details.
3. Where permitted, create or edit records through the displayed controls. Use stable, descriptive names and retain the full IDs when referencing records operationally.
4. Add or correct host mappings/inventory where those controls are available.
5. Return to the test and revalidate dependent scope selections.

Project display names improve readability; the full project ID remains the identity. Duplicate names do not make projects equivalent. A missing label is not permission to substitute a different project's ID.

## Approve a live target

For live agent-owned execution, the traffic-entry participant needs the correct current approved environment target. Inspect the URL, approval/version information, and intended audience in the environment detail. Follow the available approval workflow with an authorized operator or administrator.

Target approval is independent of merely including a participant in scope. Context-only systems do not need load-target approval, but their catalog relationships and your access still need to be valid.

The server checks approval and target version at launch and before dispatch. Changing an environment after preparing a test can invalidate readiness. Reopen Review and resolve stale checks rather than adding another target URL to a prompt or request.

## Understand ownership

The primary scope participant attributes the run to a project/application. The traffic entry selects the execution environment and native-connection project. Inventory for another participant supplies context and does not implicitly receive load.

For provider execution, local catalog relationships and remote application/release mappings must both be correct. See [APEX Load](apex-load.md). Do not assume their IDs match.

## Resolve missing or stale entries

If an application is not offered, inspect its project registration and your access. If environments fail to load, use Retry and retain the catalog error. If a record was removed or changed, correct the dependent draft rather than relying on its old label.

Catalog edits can affect future launch validation. Already launched tests retain an allowed context snapshot while authorization can still be reevaluated. Avoid deleting shared records simply to remove them from your own selector; inspect current usage and use the supported lifecycle controls.
