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
    MAX_MODEL_PHASE_OVERRIDES,
    PlaygroundPrompt,
    validate_context_packets,
    validate_context_run_input,
    validate_gate_payload,
    validate_model_by_phase,
    validate_pipeline_input,
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


def test_direct_model_allowlist_rejects_normalization_and_hostile_scalar_hooks() -> None:
    settings = ApexSettings(llm=LLMSettings(default_model="allowed", allowed_models=["allowed"]))
    calls: list[str] = []

    class HostileModel(str):
        def strip(self, *_args: object, **_kwargs: object) -> str:
            calls.append("strip")
            raise AssertionError("hostile model hook ran")

    for values in (
        {Phase.REPORTING: " allowed "},
        {Phase.REPORTING: "x" * 201},
        {Phase.REPORTING: HostileModel("allowed")},
        {str(index): "allowed" for index in range(MAX_MODEL_PHASE_OVERRIDES + 1)},
    ):
        with pytest.raises(ValueError, match="model_by_phase"):
            validate_model_by_phase(values, settings=settings)

    assert calls == []
    validate_model_by_phase({Phase.REPORTING: "allowed"}, settings=settings)


@pytest.mark.parametrize(
    "payload",
    [
        {"assistant_id": "pipeline\x00shadow"},
        {"project_id": "project\x00shadow"},
        {"connections": {"execution\x00engine": "connection"}},
        {"connections": {"execution_engine": "connection\x00shadow"}},
        {"prompt_overrides": {"phase\x00key": {"content": "safe"}}},
        {"prompt_overrides": {"phase": {"content": "unsafe\x00content"}}},
        {"pre_execution_context": ["unsafe\x00context"]},
        {"load_test": {"provider_option": "unsafe\x00value"}},
    ],
)
def test_pipeline_config_rejects_nul_before_checkpoint_persistence(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        PipelineConfigurable.model_validate(payload)


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


@pytest.mark.parametrize(
    "decision",
    [
        {"action": "discuss", "message": "Authorization: Bearer direct-secret"},
        {"action": "revise", "instructions": "password=direct-secret"},
        {"action": "approve", "note": "https://user:direct-secret@example.test"},
        {"action": "modify", "prompt": {"system": "private_key=direct-secret"}},
    ],
)
def test_gate_parser_rejects_credential_bearing_direct_langgraph_resume(
    decision: dict[str, Any],
) -> None:
    parsed = parse_gate_decision(
        decision,
        ["approve", "discuss", "modify", "revise"],
    )

    assert parsed == {
        "action": None,
        "error": "gate decision must not contain credential material",
    }
    assert "direct-secret" not in str(parsed)


@pytest.mark.parametrize("number", [nan, inf, -inf])
def test_gate_payload_rejects_nonfinite_numbers(number: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        validate_gate_payload({"action": "discuss", "score": number})


def test_gate_payload_rejects_shallow_width_at_the_node_budget() -> None:
    settings = ApexSettings(runs=RunControlSettings(max_gate_payload_nodes=16))

    with pytest.raises(ValueError, match="node limit"):
        validate_gate_payload(
            {"action": "discuss", "values": list(range(100_000))},
            settings=settings,
        )


def test_gate_parser_does_not_reflect_an_unknown_action() -> None:
    canary = "CANARY_GATE_ACTION_SECRET"

    parsed = parse_gate_decision({"action": canary}, ["approve"])

    assert parsed["action"] is None
    assert canary not in parsed["error"]


def test_rendered_model_input_has_final_budget() -> None:
    settings = ApexSettings(runs=RunControlSettings(max_model_input_chars=10_000))
    with pytest.raises(ValueError, match="rendered model input"):
        validate_rendered_model_input("s" * 5_001, "u" * 5_000, settings=settings)


def test_rendered_model_input_rejects_credentials_before_provider_call() -> None:
    secret = "model-provider-secret-canary"

    with pytest.raises(ValueError, match="credential material") as raised:
        validate_rendered_model_input(
            "safe system",
            f"fetched result: Authorization: Bearer {secret}",
        )

    assert secret not in str(raised.value)


@pytest.mark.parametrize(
    ("validator", "payload"),
    [
        (
            validate_pipeline_input,
            {"title": "direct", "request": "password=direct-pipeline-secret"},
        ),
        (
            validate_context_run_input,
            {"subject": "Authorization: Bearer direct-context-secret"},
        ),
        (
            validate_playground_run_input,
            {"prompt": {"system": "safe", "user": "password=direct-playground-secret"}},
        ),
    ],
)
def test_graph_input_validators_reject_credentials_without_auth_middleware(
    validator: Any,
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="credential material") as raised:
        validator(payload)

    assert "direct-" not in str(raised.value)


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


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "unsafe\x00title"},
        {"request": "unsafe\x00request"},
        {"project_id": "unsafe\x00project"},
        {"external_results": {"source": "unsafe\x00source"}},
        {"context_packets": [{"id": "packet", "source": "sdk", "title": "unsafe\x00title"}]},
    ],
)
def test_public_run_input_rejects_nul_before_checkpoint_persistence(
    payload: dict[str, Any],
) -> None:
    with pytest.raises((ValidationError, ValueError)):
        validate_public_run_input(payload)


@pytest.mark.parametrize(
    "uri",
    [
        "https://alice:password@results.example.com/run/42",
        "https://results.example.com/run/42?X-Amz-Signature=signed-url-secret",
        "https://results.example.com/run/42#access-token-secret",
    ],
)
def test_public_run_input_rejects_credential_bearing_external_results_uri(
    uri: str,
) -> None:
    with pytest.raises(
        ValueError,
        match="credential material|external_results is invalid",
    ) as raised:
        validate_public_run_input({"external_results": {"source": "dashboard", "uri": uri}})

    rendered = str(raised.value)
    assert "password" not in rendered
    assert "signed-url-secret" not in rendered
    assert "access-token-secret" not in rendered


def test_wrapped_pydantic_errors_do_not_reflect_rejected_values() -> None:
    canary = "CANARY_REJECTED_RUN_INPUT_SECRET"
    gate_settings = ApexSettings(runs=RunControlSettings(max_gate_string_chars=100))
    validators = [
        lambda: validate_context_packets(
            [{"id": {"secret": canary}, "source": "sdk", "title": "packet"}]
        ),
        lambda: validate_pipeline_input({"project_id": {"secret": canary}}),
        lambda: validate_context_run_input({"subject": {"secret": canary}}),
        lambda: validate_playground_run_input({"prompt": {"user": {"secret": canary}}}),
        lambda: validate_public_run_input({canary: "value"}),
        lambda: validate_context_packets(
            [
                {"id": canary, "source": "sdk", "title": "one"},
                {"id": canary, "source": "sdk", "title": "two"},
            ]
        ),
        lambda: validate_gate_payload({canary: "x" * 101}, settings=gate_settings),
    ]

    for validate in validators:
        with pytest.raises(ValueError) as raised:
            validate()
        assert canary not in str(raised.value)


@pytest.mark.parametrize(
    "validate",
    [
        lambda: validate_context_packets([{"id": "", "source": "sdk", "title": "packet"}]),
        lambda: validate_pipeline_input({"project_id": ""}),
        lambda: validate_pipeline_input({"external_results": {}}),
        lambda: validate_context_run_input({"subject": 42}),
        lambda: validate_playground_run_input({"prompt": {"user": 42}}),
    ],
)
def test_wrapped_validation_errors_detach_rejected_input_context(validate: Any) -> None:
    with pytest.raises(ValueError) as raised:
        validate()

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


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


@pytest.mark.parametrize(
    "payload",
    [
        {"gates": {"reporting": {"prompt_review": "auto", "unknown": True}}},
        {"limits": {"max_revise_loops": 1, "unknown": True}},
        {"prompt_overrides": {"phase/reporting": {"content": "safe", "unknown": True}}},
    ],
)
def test_pipeline_configurable_rejects_unknown_nested_fields(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="extra_forbidden"):
        PipelineConfigurable.model_validate(payload)


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


@pytest.mark.parametrize(
    "payload",
    [
        {"sample_input": {"key\x00shadow": "value"}},
        {"sample_input": {"key": "value\x00shadow"}},
        {"prompt": {"user": "unsafe\x00prompt"}},
    ],
)
def test_stateless_run_input_rejects_nul_json(payload: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="U\\+0000"):
        validate_playground_run_input(payload)


def test_native_input_validators_reject_primitive_and_container_subclasses_without_hooks() -> None:
    calls: list[str] = []

    class MappingBomb(dict[str, Any]):
        def __iter__(self) -> Any:
            calls.append("mapping-iter")
            raise AssertionError("mapping subclass must not be iterated")

    class ListBomb(list[Any]):
        def __iter__(self) -> Any:
            calls.append("list-iter")
            raise AssertionError("list subclass must not be iterated")

    class StringBomb(str):
        def strip(self, *_args: Any, **_kwargs: Any) -> str:
            calls.append("string-strip")
            raise AssertionError("string subclass must not be normalized")

    class IntegerBomb(int):
        def bit_length(self) -> int:
            calls.append("integer-bits")
            raise AssertionError("integer subclass must not be inspected")

    checks = (
        lambda: validate_context_run_input(MappingBomb(subject="incident")),
        lambda: validate_playground_run_input(MappingBomb(sample_input={})),
        lambda: validate_context_packets(ListBomb()),
        lambda: validate_context_packets([MappingBomb(id="packet", source="sdk", title="Packet")]),
        lambda: validate_pipeline_input({"title": StringBomb("unsafe")}),
        lambda: validate_playground_run_input({"sample_input": {"value": IntegerBomb(1)}}),
    )

    for check in checks:
        with pytest.raises(ValueError):
            check()
    assert calls == []


def test_invalid_raw_run_inputs_never_retain_credentials_on_exception_chains() -> None:
    canary = "raw-validation-secret-canary"
    checks = (
        lambda: validate_pipeline_input({"title": f"api_key={canary}" + "x" * 501}),
        lambda: validate_context_packets(
            [
                {
                    "id": "packet",
                    "source": "sdk",
                    "title": f"api_key={canary}" + "x" * 501,
                }
            ]
        ),
        lambda: validate_context_run_input({"subject": f"api_key={canary}" + "x" * 2_001}),
        lambda: validate_playground_run_input(
            {"prompt": {"system": f"api_key={canary}" + "x" * 100_001}}
        ),
    )

    for check in checks:
        with pytest.raises(ValueError) as raised:
            check()
        current: BaseException | None = raised.value
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            assert canary not in repr(current)
            current = current.__cause__ or current.__context__


def test_stateless_json_budget_rejects_oversized_integer_before_serialization() -> None:
    with pytest.raises(ValueError, match="256 bits"):
        validate_playground_run_input({"sample_input": {"value": 1 << 10_000}})


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "discuss", "message": "unsafe\x00message"},
        {"action": "modify", "prompt": {"system\x00shadow": "safe"}},
    ],
)
def test_gate_payload_rejects_nul_before_checkpoint_persistence(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="U\\+0000"):
        validate_gate_payload(payload)


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
