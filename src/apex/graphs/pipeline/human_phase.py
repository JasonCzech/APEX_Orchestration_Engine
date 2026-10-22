"""Durable human execution path, separate from approval of agent output."""

import asyncio
import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, interrupt

from apex.domain.agent_deliverables import parse_deliverable
from apex.domain.diagnostics import contains_credential_material
from apex.domain.integrations import LoadTestSpec
from apex.domain.pipeline import ExternalResults, Phase, PhaseStatus, utcnow_iso
from apex.domain.prompt_catalog import PHASE_SECTIONS
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.graphs.pipeline.gates import make_approval, resolve_actor
from apex.graphs.pipeline.state import PipelineState
from apex.services.run_validation import validate_gate_payload


def validate_completion(
    payload: dict, decision: dict, state: dict, cfg: PipelineConfigurable
) -> dict:
    validate_gate_payload(decision)
    if contains_credential_material(decision):
        raise ValueError("Completion must not contain credentials")
    if (
        decision.get("task_id") != payload["task_id"]
        or decision.get("expected_revision") != payload["revision"]
    ):
        raise ValueError("This human task has changed; refresh before completing it")
    phase = Phase(payload["phase"])
    output = decision.get("output")
    if not isinstance(output, dict):
        raise ValueError("Completed phase output is required")
    raw = json.dumps(output.get("deliverable"), separators=(",", ":"))
    deliverable = parse_deliverable(raw, phase)
    if deliverable.disposition != "complete":
        raise ValueError("Resolve the deliverable blockers before completing this phase")
    artifact_ids = output.get("artifact_ids", [])
    known = {a["id"] for a in state.get("artifacts", [])}
    if not isinstance(artifact_ids, list) or any(
        not isinstance(i, str) or i not in known for i in artifact_ids
    ):
        raise ValueError("Select artifacts belonging to this run")
    fields: dict[str, Any] = {
        "summary": deliverable.summary,
        "deliverable_json": raw,
        "artifact_ids": artifact_ids,
        "executor": "hitl",
    }
    if phase == Phase.SCRIPT_SCENARIO and cfg.executor(Phase.EXECUTION) == "langgraph":
        spec = output.get("load_test_spec")
        if not isinstance(spec, dict):
            raise ValueError("Completed execution workload is required")
        if cfg.environment_target:
            spec = {**spec, "target_environment": cfg.environment_target}
        fields["load_test_spec"] = LoadTestSpec.model_validate(spec).model_dump(mode="json")
        if (
            cfg.environment_target
            and fields["load_test_spec"]["target_environment"] != cfg.environment_target
        ):
            raise ValueError("Workload target must match the approved environment")
        if fields["load_test_spec"]["script_refs"]:
            raise ValueError(
                "Scripts must be selected through the approved provider workload; "
                "human script references cannot override that binding"
            )
    if phase == Phase.EXECUTION:
        from apex.graphs.pipeline.graph import _external_seed

        external = ExternalResults.model_validate(output.get("external_results"))
        if external.engine != cfg.engine or not external.uri or not external.kpis:
            raise ValueError("Execution requires matching engine, source URI and measured KPIs")
        imported, _ = _external_seed(external.model_dump(mode="json"), attempt=payload["revision"])
        fields.update(
            {
                k: v
                for k, v in imported.items()
                if k
                in {
                    "test_summary",
                    "external",
                    "provenance",
                    "source_uri",
                    "source_outcome_explicit",
                }
            }
        )
        native_id = output.get("native_run_id")
        if native_id is not None:
            if not isinstance(native_id, str) or not native_id.strip() or len(native_id) > 256:
                raise ValueError("Native run identity is invalid")
            fields["native_run_id"] = native_id
    if phase == Phase.REPORTING and cfg.analysis:
        supplied = output.get("analysis_results", {})
        if not isinstance(supplied, dict):
            raise ValueError("Analysis results must be keyed by connection")
        if set(supplied) != {selected.connection_id for selected in cfg.analysis.selections}:
            raise ValueError("Supply results for exactly the selected analysis connections")
        for selected in cfg.analysis.selections:
            refs = supplied.get(selected.connection_id)
            if (
                not isinstance(refs, list)
                or not refs
                or any(not isinstance(ref, str) or not ref.strip() for ref in refs)
                or len(set(refs)) != len(refs)
            ):
                raise ValueError(
                    f"Attach result evidence for {selected.tool} ({selected.connection_id})"
                )
            for ref in refs:
                if isinstance(ref, str) and ref in artifact_ids:
                    continue
                ExternalResults.model_validate({"source": selected.tool, "uri": ref})
                if ref not in deliverable.evidence:
                    raise ValueError(
                        "Include external analysis result references in the deliverable evidence"
                    )
        fields["human_analysis_results"] = supplied
    return fields


async def verify_native_execution(fields: dict, output: dict, cfg: PipelineConfigurable) -> None:
    native = (
        next((s for s in cfg.analysis.selections if s.tool == "internal_rca"), None)
        if cfg.analysis
        else None
    )
    if not native:
        # Unverified external identities cannot acquire native execution authority.
        fields.pop("native_run_id", None)
        return
    if not fields.get("native_run_id"):
        raise ValueError("Internal RCA requires the native APEX Load run ID")
    from apex.adapters.registry import PortKind
    from apex.graphs.pipeline.analysis_phase import _make_analysis_resolver
    from apex.services.connections import close_adapter

    resolver = _make_analysis_resolver()
    adapter = None
    try:
        adapter, connection_id = await resolver.resolve_with_connection_id(
            PortKind.EXECUTION_ENGINE,
            connection_id=native.connection_id,
            project_id=cfg.traffic_project_id,
            expected_provider="apex_load",
        )
        await adapter.validate_analysis_source(
            fields["native_run_id"],
            require_application=bool(cfg.traffic_app_id),
            release_id=cfg.load_test.get("release_id"),
            mapping_revision=cfg.load_test.get("mapping_revision"),
        )
        if output["external_results"]["uri"] != adapter.native_console_url(fields["native_run_id"]):
            raise ValueError("Execution source URI must be the native run's console URL")
        summary = await adapter.native_run_summary(fields["native_run_id"])
        if not summary.kpis:
            raise ValueError(
                "Native execution has no measured KPIs; complete evidence collection first"
            )
        fields["test_summary"] = summary.model_dump(mode="json")
        fields["source_outcome_explicit"] = (
            fields["test_summary"].get("passed") is not None
            or fields["test_summary"].get("outcome") is not None
        )
        fields["native_connection_id"] = connection_id
        fields["native_source_verified"] = True
    except (KeyError, RuntimeError) as exc:
        raise ValueError(
            "Native execution could not be verified. Check the run ID, connection, "
            "project, application and release binding, then retry."
        ) from exc
    finally:
        try:
            if adapter is not None:
                await close_adapter(adapter)
        finally:
            await resolver.close()


def human_nodes(phase: Phase):
    def open_task(state: PipelineState, config: RunnableConfig) -> dict:
        cfg = PipelineConfigurable.from_state_for_phase(state, config, phase)
        entry = state.get("phase_results", {})[phase.value]
        attempt = entry["attempt"]
        task = {
            "schema_version": 1,
            "kind": "human_completion",
            "phase": phase.value,
            "task_id": f"{phase.value}-a{attempt}-human",
            "revision": attempt,
            "actions": ["complete", "abort"],
            "sections": list(PHASE_SECTIONS[phase]),
            "created_at": utcnow_iso(),
            "artifact_ids": [a["id"] for a in state.get("artifacts", [])],
            "analysis_selections": cfg.analysis.model_dump(mode="json")["selections"]
            if cfg.analysis and phase == Phase.REPORTING
            else [],
            "requires_load_test_spec": phase == Phase.SCRIPT_SCENARIO
            and cfg.executor(Phase.EXECUTION) == "langgraph",
            "engine": cfg.engine,
            "approved_target": cfg.environment_target,
        }
        from apex.graphs.pipeline.phase_subgraph import emit_event

        emit_event(
            {
                "schema_version": 1,
                "type": "phase_status",
                "phase": phase.value,
                "status": "awaiting_human_completion",
                "attempt": attempt,
            }
        )
        return {
            "current_phase": phase.value,
            "phase_results": {
                phase.value: {
                    "attempt": attempt,
                    "status": PhaseStatus.AWAITING_HUMAN_COMPLETION.value,
                    "executor": "hitl",
                    "human_task": task,
                }
            },
        }

    def complete(state: PipelineState, config: RunnableConfig):
        cfg = PipelineConfigurable.from_state_for_phase(state, config, phase)
        entry = state.get("phase_results", {})[phase.value]
        payload = dict(entry["human_task"])
        if (
            payload.get("phase") != phase.value
            or payload.get("revision") != entry["attempt"]
            or payload.get("task_id") != f"{phase.value}-a{entry['attempt']}-human"
            or payload.get("sections") != list(PHASE_SECTIONS[phase])
        ):
            raise ValueError("Checkpointed human task does not match its phase and attempt")
        while True:
            decision = interrupt(payload)
            try:
                if not isinstance(decision, dict):
                    raise ValueError("Completion must be an object")
                if decision.get("action") == "abort":
                    return Command(
                        goto="finalize",
                        update={
                            "run_aborted": True,
                            "phase_results": {
                                phase.value: {
                                    "attempt": entry["attempt"],
                                    "status": "aborted",
                                    "approvals": [
                                        make_approval(
                                            phase,
                                            entry["attempt"],
                                            "human_completion",
                                            "abort",
                                            config,
                                        )
                                    ],
                                }
                            },
                        },
                    )
                if decision.get("action") != "complete":
                    raise ValueError("Choose complete or abort")
                fields = validate_completion(payload, decision, dict(state), cfg)
                if phase == Phase.EXECUTION:
                    asyncio.run(verify_native_execution(fields, decision["output"], cfg))
                fields.update(
                    status="succeeded",
                    completed_by=resolve_actor(config),
                    completed_at=utcnow_iso(),
                    attempt=entry["attempt"],
                    approvals=[
                        make_approval(
                            phase, entry["attempt"], "human_completion", "complete", config
                        )
                    ],
                )
                from datetime import datetime

                import structlog

                structlog.get_logger(__name__).info(
                    "human_phase_completed",
                    phase=phase.value,
                    attempt=entry["attempt"],
                    actor=fields["completed_by"],
                    executor="hitl",
                    wait_seconds=(
                        datetime.fromisoformat(fields["completed_at"])
                        - datetime.fromisoformat(payload["created_at"])
                    ).total_seconds(),
                )
                return Command(goto="finalize", update={"phase_results": {phase.value: fields}})
            except ValueError as exc:
                from apex.domain.diagnostics import bounded_diagnostic

                payload["error"] = bounded_diagnostic(exc, max_chars=4096)

    return open_task, complete
