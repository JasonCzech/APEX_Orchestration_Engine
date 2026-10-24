# Your first simulated test

This exercise follows a small test through authoring, review, and evidence. It uses the Simulated engine so the execution phase does not generate live target traffic. In connected mode, agent phases may still use configured models, and selected tickets or documents may still be retrieved from their providers. The offline demo makes no integration or model calls.

## Before you start

You need an operator or admin session authorized for the selected project and every scope participant. In a connected deployment, an administrator must have registered suitable applications/environments and configured the agent and artifact services required by your choices. A viewer can read the walkthrough and inspect existing runs but cannot launch it.

Use [New test](/runs/new). For the bundled offline demo, the prepared [ACC Add-a-Line Peak Certification draft](/runs/new?draft=draft-esim) supplies example scope and prompts. It initially selects APEX Load; explicitly choose **Simulated** for this exercise. Demo values are illustrative, including targets and measured results.

## Set the objective and scope

1. Enter a title such as `Practice service readiness review` and describe what you want to establish in the request.
2. In **Scope**, add exactly one backend participant. Select its project, application, and environment. A participant describes a system included in the test; it does not automatically receive traffic.
3. Mark one included participant as **Primary system under test**. This establishes the run's ownership. Add a frontend or middle systems only when useful to your objective.
4. Simulation may omit **Traffic entry**. A live agent-owned execution requires a separately approved traffic entry. See [Define test scope](scope.md).

## Configure and review the plan

1. Open **Config** and choose **Simulated**. If using the prepared ACC draft, uncheck **Internal RCA** under Reporting & Analysis tools: it belongs to the draft's original APEX Load engine and remains visible as incompatible until removed. Leave all seven phases agent-owned for this introductory exercise.
2. Choose **Review every phase**. Enter the workload and acceptance inputs required by the form; use small illustrative values and state in the request that this is a simulation.
3. In **Tickets** and **Knowledge**, include only relevant authorized sources, or leave optional selections empty.
4. In **Prompts**, inspect the initialized System Prompt and Phase context selections. Resolve missing defaults or incompatible selections. Do not place credentials in prompt text.
5. Open **Review**. Check scope, engine, owners, workload, prompts, and review counts. Follow every outstanding readiness item to its field.
6. Choose **Launch test** once. Wait for navigation to run detail. If the response is uncertain, inspect Tests and the existing launch state before trying again; see [Launch readiness](drafts-presets.md#launch-readiness).

The offline demo's reviewed plan shows Simulated, all seven agent phases, the scope roles, and configuration readiness:

![Offline demo launch preview showing the simulated engine, scope, and readiness checks](/help/images/simulated-launch-review.png)

## Work through reviews

Open **Review** or **Review pending decision** from the run overview to reach the active phase's pending review. At a prompt review, inspect the instructions and choose the execution action when ready. At an output review, inspect the summary and referenced evidence before accepting it. The available controls depend on the pending review contract.

Try asking the agent a question through **Discuss with the agent**. Discussion does not approve the result. If the output needs correction, request changes with specific revision instructions. Wait for the new result, inspect it, and then accept it. Continue through the remaining phases. A decision receipt and updated progress confirm the result.

Do not approve a blocked deliverable merely to advance the run. Resolve its blockers, revise the work, or abort. A human-owned phase instead presents structured completion requirements; [the phase guide](phases.md#complete-human-work) explains that path.

## Inspect the outcome

Open the run's phase results, Timeline, and artifacts. Identify the difference between a phase finishing and evidence being accepted. Inspect available KPIs and any threshold assessment. Simulated results do not demonstrate production performance or external-provider readiness.

Use [Tests](/runs) to find the run again. Keep its full identity when asking for help. You can reuse its setup for a new test, but revalidate scope and prompt selections before launch.

## Move to connected execution

Before changing to APEX Load or LoadRunner, follow [Configure workload & execution](configuration.md), [Configure connections](connections.md), and [Work with APEX Load](apex-load.md). A live launch needs current target approval, appropriate provider bindings, storage, authorization, and compatible analysis selections. A successful simulation establishes familiarity with the workflow; it does not certify those integrations.
