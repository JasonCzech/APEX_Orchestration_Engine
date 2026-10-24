# Concepts & navigation

APEX Orchestration coordinates performance testing from requirements through postmortem. It combines catalog context, model-assisted engineering work, execution engines, evidence, and human decisions. Start with [Your first simulated test](first-test.md) to learn the workflow before configuring a live engine.

## Tests, runs, reviews, and decisions

A **test** is the work you configure: scope, objectives, workload, instructions, and review policy. A **run** is an execution attempt with its own progress and evidence. A **review** is a paused workflow asking for input. A **decision** is the recorded result of that review. Technical details may use pipeline, thread, and gate for the underlying API objects.

Every newly authored wizard test plans seven phases: Story Analysis, Test Planning, Env Triage, Script & Scenario, Execution, Reporting & Analysis, and Postmortem. Each phase belongs to an agent or a person. Assigning a phase to a person leaves required work in the plan. It does not skip that phase. [Learn how phase ownership works](phases.md).

## Find your work

- [Home](/) summarizes operations, pending reviews, recent tests, and saved drafts. A pending-review count identifies tests waiting for a decision.
- [Tests](/runs) is the run library and entry to run detail. Open a run to review phase results, inspect evidence, or follow progress. Use New test for a new configuration.
- [Tickets](/work-items) finds requirements and work-tracking items. [Applications & environments](/environments) holds catalog context and target information.
- [Knowledge & documents](/context), [Test presets](/test-presets), and [Prompts](/prompts) supply reusable input to tests.
- [Evidence](/evidence), [Agent analytics](/analytics), and [Logs](/logs) support investigation.
- [APEX Load](/apex-load) connects the orchestration view to configured native load execution. The products maintain their own authentication and permissions.
- Test cycles appears when the deployment enables the feature. [Cycles](cycles.md) group versioned engineering deliverables and execution preparation.

Expand the desktop icon rail to see navigation labels. On compact screens use Open navigation. Settings and Help & documentation remain available from navigation. The Help for this page control opens the topic matching the current screen or wizard step.

## Roles and access

The connected dashboard authenticates with an API consumer key. A viewer can inspect authorized work. An operator can perform permitted authoring, launch, and review actions. An admin can administer permitted resources. Project and application scopes further restrict each role. A role label alone does not establish access to every project or integration.

If a project or application is absent, ask an administrator to verify both its registration and your scope. A multi-system test requires access to every participating system. Platform-wide operations, such as editing the global prompt catalog, can require an unscoped admin. Changing a browser URL does not grant access.

The offline demo provides its own simulated presenter session. A deployment's demo setting differs from a real test using the Simulated engine: the former runs the whole dashboard against bundled data; the latter exercises a connected orchestration workflow without live load generation.

## Read status accurately

Keep three facts separate: orchestration progress, execution lifecycle, and assessed acceptance. A completed run can fail an acceptance threshold. Missing measurements or incomplete assessments remain unknown. Evidence can be pending review even when it is already inspectable. See [Results, evidence & comparison](results.md).

The sidebar connection indicator describes platform reachability. It does not verify every provider, target, or workload. Fleet views may lag run detail because they poll periodically; run detail also follows a live stream. Refresh to reconcile state after a connection interruption.

## Use this guide

Search article titles and full text, or browse the five documentation areas. Copy article link includes the current section so a colleague with appropriate access can open the same explanation. Code examples have a copy control. Use your browser's print command to print an article without the navigation.

Administration topics appear for admin sessions. The API reference is a read-only description of the bundled contract. Check [Capabilities & limitations](capabilities.md) for deployment-dependent behavior and [Support](support.md) for documentation review scope.
