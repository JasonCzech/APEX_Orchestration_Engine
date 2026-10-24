# Configure workload & execution

Open [New test → Config](/runs/new?step=config) after defining scope. An operator can configure and launch within authorized scope. Live execution also depends on approved targets, provider connections, supported workloads, and artifact storage.

## Choose an execution engine

- **Simulated** uses the built-in execution simulator. It sends no live load, and offers no external analysis integrations. Connected agent phases can still invoke models and source providers.
- **APEX Load** uses the configured native load integration. Select the required application/release mapping and provider workload binding. Remote IDs belong to the provider and are not interchangeable with local catalog IDs.
- **LoadRunner** uses the configured enterprise integration and its approved workload binding. Review provider-specific readiness and analysis requirements before launch.

An engine choice is explicit. Unsupported engines retained in old presets remain visible as errors until replaced. Changing engines can leave incompatible analysis choices in place for you to resolve; the wizard does not silently drop them.

## Enter workload and acceptance criteria

Use the structured workload fields for virtual users, duration, and the supported linear ramp. Enter supported acceptance thresholds using the units and limits shown by the form. Review those exact values before launch.

Requested workload describes what you ask the engine to do. It is not a measurement, achieved throughput, or a promise of successful execution. Orchestration does not expose APEX Load's complete think-time, custom-ramp, arrival-rate, or metric-triggered hard-stop authoring controls. Configure advanced provider behavior through its supported workflow.

Acceptance is assessed from available evidence. Completion of an engine lifecycle does not imply thresholds passed. Missing metrics must remain unknown. See [Results](results.md).

## Assign phase ownership

All seven phases remain in a new wizard plan. A pressed phase control assigns that phase to LangGraph; changing it assigns the work to a person. Accessible labels identify the phase and current owner. Use Tab and the phase controls to review your choices.

A person-owned phase waits for structured output and required evidence. It does not invoke that phase's agent, engine, or analysis tools automatically. Assigning Execution or Reporting & Analysis to a person still requires the applicable result and evidence contracts. See [Complete human work](phases.md#complete-human-work).

Prompt/model readiness applies to agent-owned phases. An all-human plan needs no agent prompt selections, but still needs valid scope and human deliverables. Authored prompt choices remain in the draft if you change ownership.

## Choose the review policy

**Review every phase** pauses agent work for prompt and output reviews. **Run automatically** removes those agent review pauses. **Custom** sets prompt/output reviews individually. Human completion remains required under every policy.

In Review, inspect the counts of prompt reviews, output reviews, and human completion steps. These are distinct commitments. A blocked or invalid deliverable cannot become successful by selecting automatic review.

## Select analysis and check readiness

For a real engine, select compatible analysis connections. LRA Engine supports LoadRunner; Internal RCA supports APEX Load; PerfReportForge and Other Tools can support either through a compatible deployed gateway. Other Tools permits multiple distinct connections. [Reporting & analysis tools](analysis.md) covers requirements and recovery.

The readiness panel checks configuration and authorized connection metadata. It does not prove provider connectivity, capacity, credential validity, or final workload/release eligibility. Live launch and provider operations perform additional checks. Follow the exact outstanding fields and use Retry execution verification when available. See [Blocked launches](troubleshooting.md#launch-is-blocked).
