from math import inf, nan
from typing import Any, cast

import pytest
from langchain_core.runnables import RunnableConfig
from pydantic import ValidationError

from apex.domain.integrations import LoadTestSpec
from apex.domain.pipeline import Phase
from apex.graphs.pipeline.configurable import (
    MAX_POLL_CYCLES,
    Limits,
    PipelineConfigurable,
)
from apex.graphs.pipeline.gates import parse_gate_decision
from apex.graphs.pipeline.graph import plan_resolver
from apex.graphs.pipeline.state import PipelineState
from apex.services.run_validation import (
    PlaygroundPrompt,
    validate_context_packets,
    validate_context_run_input,
    validate_gate_payload,
    validate_playground_render_budget,
    validate_playground_run_input,
    validate_public_run_input,
    validate_rendered_model_input,
)
from apex.settings import ApexSettings, LLMSettings, RunControlSettings


@pytest.mark.parametrize(
    "payload",
    [
        {"max_revise_loops": 11},
        {"max_dialogue_turns": 101},
        {"poll_interval_s": 0},
        {"poll_interval_s": inf},
        {"poll_timeout_s": nan},
        {"poll_interval_s": 0.01, "poll_timeout_s": MAX_POLL_CYCLES + 1},
    ],
)
def test_limits_reject_unbounded_or_nonfinite_values(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Limits.model_validate(payload)


@pytest.mark.parametrize(
    "update",
    [
        {"vusers": 0},
        {"vusers": 10_001},
        {"ramp_s": inf},
        {"duration_s": nan},
        {"duration_s": 86_401},
        {"script_refs": ["x"] * 101},
        {"slas": {"error_rate": 1.1}},
        {"title": "x" * 1_001},
    ],
)
def test_load_test_spec_rejects_cost_amplification(update: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        LoadTestSpec.model_validate({"title": "bounded", **update})


def test_pipeline_config_rejects_model_outside_deployment_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = ApexSettings(llm=LLMSettings(default_model="allowed", allowed_models=["allowed"]))
    monkeypatch.setattr("apex.services.run_validation.get_settings", lambda: settings)

    with pytest.raises(ValidationError, match="APEX_LLM__ALLOWED_MODELS"):
        PipelineConfigurable(model_by_phase={Phase.REPORTING: "expensive-unapproved"})


def test_context_budget_counts_all_packet_fields() -> None:
    settings = ApexSettings(
        runs=RunControlSettings(max_context_packets=2, max_context_chars_total=1_000)
    )
    with pytest.raises(ValueError, match="rendered payload"):
        validate_context_packets(
            [
                {
                    "id": "packet-id",
                    "source": "source",
                    "title": "T" * 500,
                    "summary": "S" * 500,
                    "ref": "ref",
                    "text": "body",
                }
            ],
            settings=settings,
        )


def test_gate_budget_bounds_resume_strings_and_prompt_parts() -> None:
    settings = ApexSettings(
        runs=RunControlSettings(
            max_gate_string_chars=100,
            max_prompt_part_chars=1_000,
            max_gate_payload_chars=2_000,
        )
    )
    with pytest.raises(ValueError, match="message"):
        validate_gate_payload({"action": "discuss", "message": "x" * 101}, settings=settings)
    validate_gate_payload({"action": "modify", "prompt": {"system": "x" * 500}}, settings=settings)


def test_gate_parser_rejects_oversized_direct_langgraph_resume() -> None:
    parsed = parse_gate_decision(
        {"action": "discuss", "message": "x" * 20_001},
        ["discuss"],
    )
    assert parsed["action"] is None
    assert "exceeds" in parsed["error"]


def test_rendered_model_input_has_final_budget() -> None:
    settings = ApexSettings(runs=RunControlSettings(max_model_input_chars=10_000))
    with pytest.raises(ValueError, match="rendered model input"):
        validate_rendered_model_input("s" * 5_001, "u" * 5_000, settings=settings)


def test_plan_resolver_revalidates_direct_langgraph_context_input() -> None:
    state = cast(
        PipelineState,
        {
            "title": "direct",
            "request": "run",
            "context_packets": [
                {"id": f"p-{index}", "source": "sdk", "title": "packet"} for index in range(33)
            ],
        },
    )
    config: RunnableConfig = {
        "configurable": {
            "phases": ["story_analysis"],
            "gates": {"story_analysis": {"prompt_review": "auto", "output_review": "auto"}},
        }
    }
    with pytest.raises(ValueError, match="context_packets exceeds"):
        plan_resolver(state, config)


def test_public_run_input_rejects_server_owned_pipeline_state() -> None:
    with pytest.raises(ValueError, match=r"server-owned.*phase_results"):
        validate_public_run_input(
            {
                "title": "forged",
                "phase_results": {
                    "execution": {"status": "succeeded", "test_summary": {"passed": True}}
                },
            }
        )


def test_context_run_input_strictly_bounds_provider_fanout() -> None:
    settings = ApexSettings(runs=RunControlSettings(max_work_item_keys=2))

    with pytest.raises(ValueError, match="deployment limit"):
        validate_context_run_input(
            {"subject": "incident", "work_item_keys": ["A-1", "A-2", "A-3"]},
            settings=settings,
        )
    with pytest.raises(ValueError, match="duplicates"):
        validate_context_run_input(
            {"subject": "incident", "work_item_keys": ["A-1", "A-1"]},
            settings=settings,
        )
    with pytest.raises(ValueError, match="valid string"):
        validate_context_run_input(
            {"subject": "incident", "work_item_keys": [123]}, settings=settings
        )


def test_context_run_input_rejects_untyped_or_oversized_document_packets() -> None:
    packet = {"id": "doc-1", "source": "document", "title": "Runbook"}
    with pytest.raises(ValueError, match="extra_forbidden"):
        validate_context_run_input(
            {"subject": "incident", "document_packets": [{**packet, "nested": {"x": 1}}]}
        )

    settings = ApexSettings(runs=RunControlSettings(max_context_packets=1))
    with pytest.raises(ValueError, match="context_packets exceeds"):
        validate_context_run_input(
            {"subject": "incident", "document_packets": [packet, {**packet, "id": "doc-2"}]},
            settings=settings,
        )


def test_stateless_json_budget_bounds_bytes_nodes_depth_and_types() -> None:
    byte_settings = ApexSettings(runs=RunControlSettings(max_stateless_payload_bytes=10_000))
    with pytest.raises(ValueError, match="serialized payload"):
        validate_playground_run_input(
            {"sample_input": {"value": "x" * 10_001}}, settings=byte_settings
        )

    node_settings = ApexSettings(runs=RunControlSettings(max_stateless_payload_nodes=16))
    with pytest.raises(ValueError, match="node limit"):
        validate_playground_run_input(
            {"sample_input": {"values": list(range(16))}}, settings=node_settings
        )

    depth_settings = ApexSettings(runs=RunControlSettings(max_stateless_payload_depth=2))
    with pytest.raises(ValueError, match="nesting exceeds"):
        validate_playground_run_input(
            {"sample_input": {"outer": {"inner": "value"}}}, settings=depth_settings
        )
    with pytest.raises(ValueError, match="unsupported tuple"):
        validate_playground_run_input({"sample_input": {"value": ("not", "json")}})


def test_playground_render_budget_rejects_placeholder_amplification() -> None:
    prompt = PlaygroundPrompt(user="{value}" * 1_000)
    with pytest.raises(ValueError, match="rendered model input"):
        validate_playground_render_budget(prompt, {"value": "x" * 100})

    with pytest.raises(ValueError, match="top-level"):
        validate_playground_render_budget(PlaygroundPrompt(user="{value[name]}"), {})


def test_plan_resolver_rejects_stateless_pipeline_run() -> None:
    state = cast(PipelineState, {"title": "direct", "request": "run"})
    config: RunnableConfig = {
        "configurable": {
            "phases": ["execution"],
            "gates": {"execution": {"prompt_review": "auto", "output_review": "auto"}},
        }
    }

    with pytest.raises(ValueError, match="durable thread_id"):
        plan_resolver(state, config)
