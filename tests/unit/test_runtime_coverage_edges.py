"""Focused coverage for high-risk runtime replay and provider boundaries.

These tests exercise successful and bounded-error paths that are difficult to hit
through the full graph without real provider credentials.  Provider objects remain
fakes; the assertions are on the durable state emitted by the runtime boundary.
"""

from __future__ import annotations

import gc
import weakref
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict, Field, field_serializer

import apex.graphs.pipeline.execution_phase as execution_phase
import apex.graphs.pipeline.phase_subgraph as phase_subgraph
from apex.domain.diagnostics import (
    bounded_diagnostic,
    contains_credential_material,
    is_credential_field,
)
from apex.domain.pipeline import EngineHandle, Phase
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.graphs.pipeline.state import PipelineState
from apex.ports.artifact_store import StoredArtifact, engine_artifact_namespace


def _runtime_settings(*, tool_rounds: int = 1, locked_down: bool = True) -> Any:
    return SimpleNamespace(
        is_locked_down=locked_down,
        documents=SimpleNamespace(max_context_chars_total=50_000),
        runs=SimpleNamespace(
            max_context_chars_total=50_000,
            max_gate_string_chars=50_000,
            max_model_input_chars=100_000,
            max_prompt_part_chars=100_000,
        ),
        llm=SimpleNamespace(
            adaptive_thinking=True,
            anthropic_api_key="test-provider-key",
            default_model="test-model",
            fetch_allow_private_hosts=False,
            fetch_allowed_hosts=("results.example.com",),
            fetch_max_bytes=8_192,
            fetch_max_tool_iters=tool_rounds,
            fetch_timeout_s=1.0,
            fetch_tool_enabled=True,
            max_tokens=256,
            timeout_s=2.0,
        ),
    )


def _prompt_review() -> dict[str, Any]:
    return {
        "system": "You are a test analyst.",
        "phase_prompt": "Analyze the supplied result.",
        "application": "Checkout application",
        "additional_context": "",
        "source": {"origin": "catalog", "ref": "phase/story_analysis@v1"},
        "updated_at": "2026-01-01T00:00:00+00:00",
        "updated_by": "system",
    }


def test_llm_agent_tool_loop_is_bounded_and_emits_durable_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real tool round and the configured round-limit fallback stay bounded."""

    settings = _runtime_settings(tool_rounds=1)
    monkeypatch.setattr(phase_subgraph, "get_settings", lambda: settings)

    fetched: list[tuple[str, int]] = []

    def fake_fetch(url: str, **kwargs: Any) -> str:
        fetched.append((url, kwargs["max_bytes"]))
        return "p95=123ms"

    monkeypatch.setattr("apex.services.results_fetch.fetch_results_text", fake_fetch)

    approved_url = "https://results.example.com/run/42"
    first = SimpleNamespace(
        content="",
        tool_calls=[
            {
                "id": "fetch-1",
                "name": "fetch_results",
                "args": {"url": approved_url},
            }
        ],
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "input_token_details": {"cache_read": 3},
        },
    )
    second = SimpleNamespace(
        content="",
        tool_calls=[
            {
                "id": "fetch-2",
                "name": "fetch_results",
                "args": {"url": approved_url},
            }
        ],
        usage_metadata={"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
    )
    final = SimpleNamespace(
        content=[
            {"type": "thinking", "thinking": "private reasoning"},
            {"type": "text", "text": "The run is healthy."},
        ],
        tool_calls=[],
        usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
    )

    class FakeChatAnthropic:
        init_kwargs: dict[str, Any] = {}
        bound_tools: list[Any] = []
        invocations: list[list[Any]] = []

        def __init__(self, **kwargs: Any) -> None:
            type(self).init_kwargs = kwargs
            self.responses = [first, second, final]

        def bind_tools(self, tools: list[Any]) -> FakeChatAnthropic:
            type(self).bound_tools = tools
            return self

        def invoke(self, messages: list[Any]) -> Any:
            type(self).invocations.append(list(messages))
            return self.responses.pop(0)

    monkeypatch.setattr("langchain_anthropic.ChatAnthropic", FakeChatAnthropic)

    story_entry: dict[str, Any] = {"attempt": 1}
    state = cast(
        PipelineState,
        {
            "title": "Checkout run",
            "request": "Summarize",
            "prompt_reviews": {Phase.STORY_ANALYSIS.value: _prompt_review()},
            "phase_results": {
                Phase.STORY_ANALYSIS.value: story_entry,
                Phase.EXECUTION.value: {
                    "external": True,
                    "source_uri": approved_url,
                },
            },
        },
    )
    run_config = cast(
        RunnableConfig,
        {
            "configurable": {
                "thread_id": "runtime-coverage-agent",
                "agent_backend": "anthropic",
            }
        },
    )
    cfg = PipelineConfigurable.from_config(run_config)

    update = phase_subgraph._llm_agent_body(
        Phase.STORY_ANALYSIS,
        state,
        run_config,
        cfg,
        story_entry,
        1,
    )

    entry = update["phase_results"][Phase.STORY_ANALYSIS.value]
    assert entry["summary"] == "The run is healthy."
    assert entry["model"] == "test-model"
    assert entry["usage_metadata"] == {
        "input_tokens": 17,
        "output_tokens": 7,
        "total_tokens": 24,
        "input_token_details": {"cache_read": 3},
    }
    assert [call["id"] for call in entry["tool_calls"]] == ["fetch-1"]
    assert fetched and fetched[0][0] == approved_url
    assert FakeChatAnthropic.init_kwargs["thinking"] == {"type": "adaptive"}
    assert [tool.name for tool in FakeChatAnthropic.bound_tools] == ["fetch_results"]
    # Provider content is never retained for the next request. Only the bounded,
    # canonical tool-call protocol envelope crosses that boundary.
    first_followup = FakeChatAnthropic.invocations[1]
    assistant = first_followup[-2]
    assert assistant.content == ""
    assert assistant.tool_calls == [
        {
            "name": "fetch_results",
            "args": {"url": approved_url},
            "id": "fetch-1",
            "type": "tool_call",
        }
    ]
    assert all(message is not first for message in first_followup)


def test_llm_tool_loop_rejects_raw_response_amplification_before_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _runtime_settings(tool_rounds=1)
    settings.runs.max_model_input_chars = 2_000
    monkeypatch.setattr(phase_subgraph, "get_settings", lambda: settings)

    approved_url = "https://results.example.com/run/42"

    class ProviderResponse:
        content: str
        tool_calls: list[dict[str, Any]]
        usage_metadata: dict[str, Any]

    raw_response = ProviderResponse()
    raw_response.content = "provider-output" * 1_000_000
    raw_response.tool_calls = [
        {
            "id": "fetch-1",
            "name": "fetch_results",
            "args": {"url": approved_url},
            "ignored_provider_blob": "x" * 1_000_000,
        }
    ]
    raw_response.usage_metadata = {}
    raw_response_ref = weakref.ref(raw_response)
    responses: list[Any] = [
        raw_response,
        SimpleNamespace(content="done", tool_calls=[], usage_metadata={}),
    ]
    del raw_response

    class FakeChatAnthropic:
        invocations: list[list[Any]] = []

        def __init__(self, **_kwargs: Any) -> None:
            pass

        def bind_tools(self, _tools: list[Any]) -> FakeChatAnthropic:
            return self

        def invoke(self, messages: list[Any]) -> Any:
            type(self).invocations.append(list(messages))
            if len(type(self).invocations) == 2:
                gc.collect()
                assert raw_response_ref() is None
            return responses.pop(0)

    monkeypatch.setattr("langchain_anthropic.ChatAnthropic", FakeChatAnthropic)
    monkeypatch.setattr(
        "apex.services.results_fetch.fetch_results_text",
        lambda *_args, **_kwargs: "ok",
    )
    state = cast(
        PipelineState,
        {
            "title": "Checkout run",
            "request": "Summarize",
            "prompt_reviews": {Phase.STORY_ANALYSIS.value: _prompt_review()},
            "phase_results": {
                Phase.STORY_ANALYSIS.value: {"attempt": 1},
                Phase.EXECUTION.value: {"external": True, "source_uri": approved_url},
            },
        },
    )
    run_config = cast(
        RunnableConfig,
        {
            "configurable": {
                "thread_id": "runtime-provider-amplification",
                "agent_backend": "anthropic",
            }
        },
    )

    update = phase_subgraph._llm_agent_body(
        Phase.STORY_ANALYSIS,
        state,
        run_config,
        PipelineConfigurable.from_config(run_config),
        {"attempt": 1},
        1,
    )

    assert update["phase_results"][Phase.STORY_ANALYSIS.value]["summary"] == "done"
    assert len(FakeChatAnthropic.invocations) == 2
    followup = FakeChatAnthropic.invocations[1]
    assert raw_response_ref() is None
    assert sum(len(str(message.content)) for message in followup) <= 2_000


def test_fetch_tool_contract_rejects_unsupplied_url_and_redacts_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _runtime_settings()
    approved = frozenset({"https://results.example.com/run/42"})

    def fake_fetch(url: str, **_kwargs: Any) -> str:
        if url.endswith("42"):
            return "ok"
        raise AssertionError("unapproved URL reached the transport")

    monkeypatch.setattr("apex.services.results_fetch.fetch_results_text", fake_fetch)
    (tool,) = phase_subgraph._build_agent_tools(settings, approved_urls=approved)

    assert tool.invoke({"url": next(iter(approved))}) == "ok"
    rejected = tool.invoke({"url": "https://results.example.com/run/other"})
    assert rejected.startswith("error: FetchError:")
    assert "results URL was not supplied by this run" in rejected


def test_locked_anthropic_agent_fails_closed_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _runtime_settings()
    settings.llm.anthropic_api_key = None
    monkeypatch.setattr(phase_subgraph, "get_settings", lambda: settings)

    agent = phase_subgraph._make_agent(Phase.STORY_ANALYSIS)
    update = agent(
        {"phase_results": {Phase.STORY_ANALYSIS.value: {"attempt": 3}}},
        {
            "configurable": {
                "thread_id": "locked-anthropic",
                "agent_backend": "anthropic",
            }
        },
    )

    entry = update["phase_results"][Phase.STORY_ANALYSIS.value]
    assert entry["status"] == "failed"
    assert entry["attempt"] == 3
    assert "Anthropic API key" in entry["reasoning_digest"]


def test_anthropic_agent_failure_is_bounded_and_credential_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _runtime_settings()
    monkeypatch.setattr(phase_subgraph, "get_settings", lambda: settings)
    secret = "agent-provider-secret-canary"

    def fail_agent(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"Authorization: Bearer {secret}")

    monkeypatch.setattr(phase_subgraph, "_llm_agent_body", fail_agent)
    agent = phase_subgraph._make_agent(Phase.STORY_ANALYSIS)
    update = agent(
        cast(
            PipelineState,
            {"phase_results": {Phase.STORY_ANALYSIS.value: {"attempt": 2}}},
        ),
        cast(
            RunnableConfig,
            {
                "configurable": {
                    "thread_id": "agent-error-redaction",
                    "agent_backend": "anthropic",
                }
            },
        ),
    )

    entry = update["phase_results"][Phase.STORY_ANALYSIS.value]
    assert entry["status"] == "failed"
    assert entry["attempt"] == 2
    assert secret not in entry["reasoning_digest"]
    assert secret not in entry["errors"][0]
    assert "[REDACTED]" in entry["reasoning_digest"]


def test_review_gates_fail_durably_after_malformed_resume_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict[str, Any]] = []

    def malformed_resume(payload: dict[str, Any]) -> dict[str, str]:
        payloads.append(payload)
        return {"action": "unsupported"}

    monkeypatch.setattr(phase_subgraph, "interrupt", malformed_resume)
    gate_config = cast(
        RunnableConfig,
        {
            "configurable": {
                "thread_id": "review-loop-cap",
                "gates": {
                    Phase.STORY_ANALYSIS.value: {
                        "prompt_review": "gated",
                        "output_review": "gated",
                    }
                },
            }
        },
    )
    prompt_state = cast(
        PipelineState,
        {
            "phase_results": {Phase.STORY_ANALYSIS.value: {"attempt": 2}},
            "prompt_reviews": {Phase.STORY_ANALYSIS.value: _prompt_review()},
        },
    )

    prompt_result = phase_subgraph._make_prompt_gate(Phase.STORY_ANALYSIS)(
        prompt_state,
        gate_config,
    )
    assert prompt_result.goto == "finalize"
    assert prompt_result.update is not None
    prompt_entry = prompt_result.update["phase_results"][Phase.STORY_ANALYSIS.value]
    assert prompt_entry["status"] == "failed"
    assert "loop cap" in prompt_entry["errors"][0]
    assert payloads[-1]["error"].startswith("unknown action")

    payloads.clear()
    output_state = cast(
        PipelineState,
        {
            "phase_results": {
                Phase.STORY_ANALYSIS.value: {
                    "attempt": 2,
                    "summary": "safe summary",
                    "reasoning_digest": "safe reasoning",
                }
            }
        },
    )
    output_result = phase_subgraph._make_output_gate(Phase.STORY_ANALYSIS)(
        output_state,
        gate_config,
    )
    assert output_result.goto == "finalize"
    assert output_result.update is not None
    output_entry = output_result.update["phase_results"][Phase.STORY_ANALYSIS.value]
    assert output_entry["status"] == "failed"
    assert "loop cap" in output_entry["errors"][0]
    assert payloads[-1]["error"].startswith("unknown action")


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "None"),
        (True, "True"),
        (False, "False"),
        (123, "123"),
        (1.25, "1.25"),
        (b"bytes", "bytes"),
        (bytearray(b"array"), "array"),
        (date(2026, 1, 2), "2026-01-02"),
        (datetime(2026, 1, 2, tzinfo=UTC), "2026-01-02"),
        (time(1, 2, 3), "01:02:03"),
        (timedelta(seconds=2), "0:00:02"),
        (UUID(int=0), "00000000"),
    ],
)
def test_bounded_diagnostic_exact_builtin_rendering(value: Any, expected: str) -> None:
    assert expected in bounded_diagnostic(value, max_chars=128)


def test_bounded_diagnostic_handles_enum_exception_args_and_huge_integer() -> None:
    class Label(Enum):
        SAFE = "safe-label"

    assert bounded_diagnostic(Label.SAFE) == "safe-label"
    rendered = bounded_diagnostic(ValueError("ordinary", 7, b"bytes", object()), max_chars=128)
    assert rendered.startswith("ValueError: ordinary, 7, bytes")
    assert "object" not in rendered
    assert "integer diagnostic unavailable" in bounded_diagnostic(1 << 100_000)
    with pytest.raises(ValueError, match="positive"):
        bounded_diagnostic("x", max_chars=0)


def test_credential_scanner_handles_aliases_extras_serializers_and_cycles() -> None:
    class AliasedModel(BaseModel):
        model_config = ConfigDict(extra="allow")

        ordinary: str = Field(alias="ordinaryAlias", serialization_alias="ordinaryOutput")

    safe_model = AliasedModel.model_validate({"ordinaryAlias": "safe", "extension": "value"})
    assert contains_credential_material(safe_model) is False

    unsafe_extra = AliasedModel.model_validate(
        {"ordinaryAlias": "safe", "providerPassword": "secret"}
    )
    assert contains_credential_material(unsafe_extra) is True

    class SerializedModel(BaseModel):
        ordinary: str

        @field_serializer("ordinary")
        def serialize_ordinary(self, value: str) -> str:
            return value

    assert contains_credential_material(SerializedModel(ordinary="safe")) is True

    cyclic: dict[str, Any] = {"ordinary": "safe"}
    cyclic["cycle"] = cyclic
    assert contains_credential_material(cyclic) is False

    with pytest.raises(ValueError, match="limits"):
        contains_credential_material("safe", max_nodes=0)


@dataclass(slots=True)
class _SlottedEvidence:
    ordinary: str


def test_credential_scanner_accepts_safe_scalar_and_slotted_domain_values() -> None:
    assert (
        contains_credential_material(
            [
                Decimal("1.5"),
                date(2026, 1, 1),
                datetime(2026, 1, 1, tzinfo=UTC),
                time(1, 2),
                timedelta(seconds=1),
                UUID(int=1),
                _SlottedEvidence("safe"),
            ]
        )
        is False
    )


def test_credential_field_wrappers_and_nested_assignments_are_redacted() -> None:
    assert is_credential_field(cast(Any, 7)) is True
    assert is_credential_field("x" * 1_025) is True
    assert is_credential_field("authenticationModeValue") is False
    assert is_credential_field("providerApiKeyValue") is True

    secret = "nested-diagnostic-secret"
    rendered = bounded_diagnostic(
        f'{{"apiKey":"{secret}","detail":"token={secret}"}}',
        max_chars=512,
    )
    assert secret not in rendered
    assert rendered.count("[REDACTED]") >= 2


def test_diagnostic_metadata_and_exception_rendering_fail_safe() -> None:
    class NoSafeArguments(ValueError):
        pass

    assert "diagnostic unavailable" in bounded_diagnostic(NoSafeArguments(object()))
    assert bounded_diagnostic(ValueError("x" * 100), max_chars=4).startswith("Valu")

    import apex.domain.diagnostics as diagnostics

    class ShadowingMeta(type):
        @property
        def __name__(cls) -> str:  # type: ignore[reportIncompatibleVariableOverride]  # noqa: N805
            raise AssertionError("metadata descriptor must not run")

    class Opaque(metaclass=ShadowingMeta):
        pass

    assert diagnostics._safe_type_name(Opaque()) == "unknown"
    assert diagnostics.safe_type_name(Opaque()) == "unknown"
    with pytest.raises(TypeError, match="field metadata"):
        diagnostics._raw_field_info_value(object(), "alias")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="decorator metadata"):
        diagnostics._raw_pydantic_decorator_map(object(), "field_serializers")
    with pytest.raises(TypeError, match="slot name"):
        diagnostics._raw_slot_field(object(), "")
    with pytest.raises(TypeError, match="field name"):
        diagnostics._raw_dataclass_field(object(), 7)
    with pytest.raises(TypeError, match="instance field"):
        diagnostics._raw_descriptor_value(object(), object, "missing")
    with pytest.raises(TypeError, match="no dictionary"):
        diagnostics._raw_instance_dict(7)


def test_credential_scanner_fails_closed_on_hostile_or_oversized_shapes() -> None:
    class ScalarSubclass(str):
        pass

    class CustomMapping(Mapping[str, Any]):
        def __getitem__(self, key: str) -> Any:
            raise AssertionError(key)

        def __iter__(self) -> Any:
            raise AssertionError("must not iterate")

        def __len__(self) -> int:
            return 1

    class ModelLike:
        def model_dump(self) -> Any:
            raise AssertionError("must not serialize")

    class InvalidSlots:
        __slots__ = ["ordinary"]

        def __init__(self) -> None:
            self.ordinary = "safe"

    class OrdinaryObject:
        def __init__(self, value: str) -> None:
            self.ordinary = value

    @dataclass
    class OrdinaryDataclass:
        ordinary: str

    assert contains_credential_material({7: "safe"}) is True
    assert contains_credential_material("abcd", max_total_chars=3) is True
    assert contains_credential_material([1, 2], max_nodes=1) is True
    assert contains_credential_material(ScalarSubclass("safe")) is True
    assert contains_credential_material(CustomMapping()) is True
    assert contains_credential_material(ModelLike()) is True
    assert contains_credential_material(InvalidSlots()) is True
    assert contains_credential_material(OrdinaryObject("safe")) is False
    assert contains_credential_material(OrdinaryObject("password=unsafe")) is True
    assert contains_credential_material(OrdinaryDataclass("safe")) is False


def test_agent_response_and_tool_argument_boundaries_fail_closed() -> None:
    assert phase_subgraph._message_text({"content": object()}) == ""
    assert phase_subgraph._message_text({"content": [" first ", {}, "", "second"]}) == (
        "first\nsecond"
    )
    assert phase_subgraph._response_tool_calls({}) == []
    with pytest.raises(ValueError, match="objects"):
        phase_subgraph._response_tool_calls({"tool_calls": [object()]})

    assert phase_subgraph._tool_call_text(None, label="id", default="fallback") == "fallback"
    for invalid in ("", "x\x00y", "x" * 257, 7, "password=unsafe"):
        with pytest.raises(ValueError, match="bounded credential-free"):
            phase_subgraph._tool_call_text(invalid, label="id", default="fallback")

    assert phase_subgraph._tool_call_args(None) == {}
    invalid_args: list[tuple[Any, str]] = [
        ([], "must be an object"),
        ({f"field-{index}": index for index in range(17)}, "too many fields"),
        ({"": "value"}, "names are invalid"),
        ({"items": list(range(17))}, "too many items"),
        ({"value": "bad\x00value"}, "U\\+0000"),
        ({"value": 1 << 300}, "oversized integer"),
        ({"value": float("inf")}, "unsupported values"),
        ({"value": object()}, "unsupported values"),
        ({"nested": {"a": {"b": {"c": {"d": "too deep"}}}}}, "structural limit"),
    ]
    for value, message in invalid_args:
        with pytest.raises(ValueError, match=message):
            phase_subgraph._tool_call_args(value)


def test_agent_prompt_composition_and_fetch_authority_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _runtime_settings()
    monkeypatch.setattr(phase_subgraph, "get_settings", lambda: settings)

    assert phase_subgraph._compose_system({}) == "You are an APEX analysis agent."
    assert phase_subgraph._compose_system({"application": "app only"}) == "app only"
    for invalid in ([], {"system": 7}, {"application": 7}):
        with pytest.raises(ValueError, match="resolved prompt"):
            phase_subgraph._compose_system(invalid)  # type: ignore[arg-type]

    assert phase_subgraph._compose_user({}, Phase.STORY_ANALYSIS, {}, {}) == (
        "(no request provided)"
    )
    revised = phase_subgraph._compose_user(
        {},
        Phase.STORY_ANALYSIS,
        {"user": "analyze"},
        {"revise_instructions": "focus on latency"},
    )
    assert "Operator revision instructions: focus on latency" in revised
    for resolved, entry in (([], {}), ({"user": 7}, {}), ({}, {"revise_instructions": 7})):
        with pytest.raises(ValueError):
            phase_subgraph._compose_user(
                {},
                Phase.STORY_ANALYSIS,
                resolved,  # type: ignore[arg-type]
                entry,
            )

    assert phase_subgraph._approved_fetch_urls({}) == frozenset()
    assert (
        phase_subgraph._approved_fetch_urls(
            {"phase_results": {Phase.EXECUTION.value: {"external": True, "source_uri": "bad"}}}
        )
        == frozenset()
    )
    for state in (
        {"phase_results": []},
        {"phase_results": {Phase.EXECUTION.value: []}},
    ):
        with pytest.raises(ValueError):
            phase_subgraph._approved_fetch_urls(cast(PipelineState, state))

    settings.llm.fetch_tool_enabled = False
    assert phase_subgraph._build_agent_tools(settings, approved_urls=frozenset({"https://x"})) == []
    settings.llm.fetch_tool_enabled = True
    settings.llm.fetch_allowed_hosts = ()
    assert phase_subgraph._build_agent_tools(settings, approved_urls=frozenset({"https://x"})) == []


def test_agent_tool_invocation_and_preview_remain_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _runtime_settings()

    class OrdinaryTool:
        name = "ordinary"

        def invoke(self, args: dict[str, Any]) -> dict[str, Any]:
            return {"echo": args}

    assert "budget exhausted" in phase_subgraph._invoke_agent_tool(
        OrdinaryTool(),
        {},
        settings=settings,
        remaining_chars=256,
        approved_urls=frozenset(),
    )
    assert phase_subgraph._invoke_agent_tool(
        OrdinaryTool(),
        {"value": 3},
        settings=settings,
        remaining_chars=1_000,
        approved_urls=frozenset(),
    ) == {"echo": {"value": 3}}

    class FetchTool:
        name = "fetch_results"

    result = phase_subgraph._invoke_agent_tool(
        FetchTool(),
        {"url": "https://results.example.com/not-approved"},
        settings=settings,
        remaining_chars=1_000,
        approved_urls=frozenset({"https://results.example.com/approved"}),
    )
    assert "not supplied" in result

    cyclic: dict[str, Any] = {}
    cyclic["cycle"] = cyclic
    preview = phase_subgraph._safe_tool_args(
        cast(
            dict[str, Any],
            {
                "password": "secret",
                "url": "https://user:pass@example.com/path?token=secret",
                "huge": 1 << 300,
                "not_finite": float("nan"),
                "deep": {"a": {"b": {"c": {"d": "hidden"}}}},
                "cycle": cyclic,
                "many": list(range(20)),
                7: object(),
            },
        )
    )
    assert preview["password"] == "[REDACTED]"
    assert "user:pass" not in preview["url"]
    assert preview["huge"] == "…[integer omitted]"
    assert preview["not_finite"] == "…[non-finite number]"
    assert preview["deep"]["a"]["b"]["c"] == "…[max depth]"
    assert preview["cycle"]["cycle"] == "…[cycle]"
    assert preview["many"][-1] == "…[more items]"
    assert "[non-string-key]" in preview


def test_usage_metadata_detail_cardinality_and_names_are_bounded() -> None:
    acc: dict[str, Any] = {"input_token_details": {f"existing-{index}": 1 for index in range(64)}}
    details = {
        "providerToken": 100,
        "": 100,
        **{f"new-{index}": index for index in range(140)},
    }
    phase_subgraph._accumulate_usage(
        acc,
        {
            "input_tokens": 2,
            "input_token_details": details,
        },
    )
    assert acc["input_tokens"] == 2
    assert len(acc["input_token_details"]) == 64
    assert "providerToken" not in acc["input_token_details"]


def test_phase_checkpoint_identity_and_prompt_boundaries() -> None:
    assert phase_subgraph._prompt_variables({}) == {
        "title": "untitled run",
        "request": "(no request provided)",
    }
    for state in (
        {"title": 7},
        {"request": "bad\x00request"},
        {"title": "password=unsafe"},
    ):
        with pytest.raises(ValueError):
            phase_subgraph._prompt_variables(cast(PipelineState, state))

    assert phase_subgraph._entry({}, Phase.STORY_ANALYSIS) == {}
    assert phase_subgraph._attempt({}) == 1
    for state in (
        {"phase_results": []},
        {"phase_results": {Phase.STORY_ANALYSIS.value: []}},
    ):
        with pytest.raises(ValueError):
            phase_subgraph._entry(cast(PipelineState, state), Phase.STORY_ANALYSIS)
    with pytest.raises(ValueError, match="attempt"):
        phase_subgraph._attempt({"attempt": True})

    assert (
        phase_subgraph._dialogue_matches_attempt(
            {"phase": Phase.STORY_ANALYSIS.value},
            Phase.STORY_ANALYSIS,
            1,
        )
        is True
    )
    assert (
        phase_subgraph._dialogue_matches_attempt(
            {"phase": Phase.REPORTING.value, "attempt": 1},
            Phase.STORY_ANALYSIS,
            1,
        )
        is False
    )
    for entry in ([], {"phase": 7}):
        with pytest.raises(ValueError, match="dialogue"):
            phase_subgraph._dialogue_matches_attempt(
                entry,  # type: ignore[arg-type]
                Phase.STORY_ANALYSIS,
                1,
            )

    assert (
        phase_subgraph._thread_id(
            cast(RunnableConfig, {"configurable": {"thread_id": "durable-thread"}})
        )
        == "durable-thread"
    )
    for config in ([], {"configurable": []}, {"configurable": {}}):
        with pytest.raises(ValueError):
            phase_subgraph._thread_id(config)  # type: ignore[arg-type]


def test_prompt_review_and_legacy_resolution_revalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _runtime_settings()
    monkeypatch.setattr(phase_subgraph, "get_settings", lambda: settings)
    valid = _prompt_review()
    validated, resolved = phase_subgraph._validated_prompt_review(valid)
    assert validated == valid
    assert resolved["user"] == valid["phase_prompt"]

    invalid_reviews = (
        [],
        {**valid, "unknown": "field"},
        {**valid, "system": 7},
        {**valid, "application": 7},
        {**valid, "source": []},
        {**valid, "source": {"origin": "catalog", "ref": 7}},
    )
    for review in invalid_reviews:
        with pytest.raises(ValueError, match="prompt review"):
            phase_subgraph._validated_prompt_review(review)

    canary = "bare-prompt-resolution-canary"

    def fail_resolution(_review: object) -> None:
        raise ValueError(canary)

    original_resolver = phase_subgraph.resolved_from_prompt_review
    monkeypatch.setattr(phase_subgraph, "resolved_from_prompt_review", fail_resolution)
    with pytest.raises(ValueError, match="prompt review is invalid") as raised:
        phase_subgraph._validated_prompt_review(valid)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert canary not in str(raised.value)
    monkeypatch.setattr(phase_subgraph, "resolved_from_prompt_review", original_resolver)

    cfg = PipelineConfigurable(project_id="project-1", app_id="app-1")
    assert phase_subgraph._state_application_override({}, cfg) is None
    assert (
        phase_subgraph._state_application_override(
            {"application_reviews": {"app-1": {"content": "shared context"}}},
            cfg,
        )
        == "shared context"
    )
    for state in (
        {"application_reviews": []},
        {"application_reviews": {"app-1": []}},
        {"application_reviews": {"app-1": {"content": 7}}},
    ):
        with pytest.raises(ValueError, match="application prompt override"):
            phase_subgraph._state_application_override(cast(PipelineState, state), cfg)

    legacy_state = cast(
        PipelineState,
        {
            "phase_results": {
                Phase.STORY_ANALYSIS.value: {
                    "resolved_prompt": {"application": None},
                }
            }
        },
    )
    review, legacy_resolved = phase_subgraph._review_source_for_phase(
        legacy_state,
        Phase.STORY_ANALYSIS,
        cfg,
    )
    assert review["source"] == {"origin": "catalog"}
    assert legacy_resolved["system"] == ""
    assert legacy_resolved["user"] == ""
    for prompt_entry in (
        {"resolved_prompt": []},
        {"resolved_prompt": {"system": 7, "user": "safe"}},
        {
            "resolved_prompt": {"system": "safe", "user": "safe"},
            "resolved_prompt_source": [],
        },
    ):
        state = cast(
            PipelineState,
            {"phase_results": {Phase.STORY_ANALYSIS.value: prompt_entry}},
        )
        with pytest.raises(ValueError):
            phase_subgraph._review_source_for_phase(state, Phase.STORY_ANALYSIS, cfg)


def test_checkpoint_summary_artifact_and_dialogue_projections_fail_closed() -> None:
    valid_summary = {
        "engine": "sim",
        "passed": True,
        "kpis": {"p95": 123.0},
        "sla_breaches": [],
        "notes": None,
    }
    assert phase_subgraph._validated_checkpoint_summary(valid_summary) is not None
    invalid_summaries = (
        {**valid_summary, "extra": 1},
        {**valid_summary, "engine": ""},
        {**valid_summary, "kpis": {"": 1}},
        {**valid_summary, "kpis": {"huge": 1_000_000_000_001}},
        {**valid_summary, "kpis": {"nan": float("nan")}},
        {**valid_summary, "kpis": {"text": "fast"}},
        {**valid_summary, "sla_breaches": [7]},
        {**valid_summary, "notes": "password=unsafe"},
    )
    for summary in invalid_summaries:
        assert phase_subgraph._validated_checkpoint_summary(summary) is None

    artifact_entry = {"artifact_ids": ["artifact-1"]}
    artifact_state = cast(
        PipelineState,
        {
            "artifacts": [
                {"id": "other", "kind": "report", "name": "ignored"},
                {"id": "artifact-1", "kind": "report", "name": "selected"},
            ]
        },
    )
    assert phase_subgraph._checkpoint_artifact_previews(artifact_state, artifact_entry) == [
        {"id": "artifact-1", "kind": "report", "name": "selected"}
    ]
    for state, entry in (
        ({}, {"artifact_ids": [7]}),
        ({"artifacts": {}}, artifact_entry),
        ({"artifacts": [7]}, artifact_entry),
        ({"artifacts": [{"id": 7}]}, artifact_entry),
        (
            {"artifacts": [{"id": "artifact-1", "kind": 7, "name": "bad"}]},
            artifact_entry,
        ),
    ):
        with pytest.raises(ValueError):
            phase_subgraph._checkpoint_artifact_previews(
                cast(PipelineState, state),
                entry,
            )

    dialogue_entry = {
        "id": "dialogue-1",
        "phase": Phase.STORY_ANALYSIS.value,
        "attempt": 2,
        "role": "operator",
        "content": "please explain",
        "at": "2026-01-01T00:00:00+00:00",
    }
    assert phase_subgraph._checkpoint_phase_dialogue(
        {"dialogue": [dialogue_entry]},
        Phase.STORY_ANALYSIS,
        2,
    ) == [dialogue_entry]
    for raw in ({}, [7], [{**dialogue_entry, "extra": 1}], [{**dialogue_entry, "role": "bad"}]):
        state = {} if raw == {} else {"dialogue": raw}
        if raw == {}:
            assert (
                phase_subgraph._checkpoint_phase_dialogue(
                    cast(PipelineState, state),
                    Phase.STORY_ANALYSIS,
                    2,
                )
                == []
            )
        else:
            with pytest.raises(ValueError, match="dialogue"):
                phase_subgraph._checkpoint_phase_dialogue(
                    cast(PipelineState, state),
                    Phase.STORY_ANALYSIS,
                    2,
                )


async def test_engine_artifact_write_only_view_rejects_unsafe_operations() -> None:
    namespace = engine_artifact_namespace("runtime-coverage-artifacts")
    key = f"{namespace}/provider-name.json"

    class Store:
        async def put(self, stored_key: str, data: bytes, **_kwargs: Any) -> StoredArtifact:
            return StoredArtifact(
                key=stored_key,
                uri=f"memory://{stored_key}",
                size=len(data),
            )

        async def delete(self, _key: str) -> None:
            return None

    view = execution_phase._EngineArtifactStoreView(Store(), namespace)
    with pytest.raises(ValueError, match="data must be bytes"):
        await view.put(key, bytearray(b"not exact bytes"), content_type="application/json")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="content type"):
        await view.put(key, b"body", content_type="text/plain\nforged")

    stored = await view.put(key, b"body", content_type="application/json")
    assert stored.key.endswith("artifact-0000")
    with pytest.raises(ValueError, match="attempted only once"):
        await view.put(key, b"body", content_type="application/json")
    with pytest.raises(RuntimeError, match="write-only"):
        await view.get(key)
    with pytest.raises(RuntimeError, match="write-only"):
        view.iter_bytes(key)
    with pytest.raises(RuntimeError, match="write-only"):
        await view.get_url(key)
    with pytest.raises(RuntimeError, match="cannot delete"):
        await view.delete(key)


async def test_engine_artifact_stream_requires_complete_bounded_consumption() -> None:
    namespace = engine_artifact_namespace("runtime-coverage-stream")
    key = f"{namespace}/stream.bin"
    deleted: list[str] = []

    async def chunks() -> AsyncIterator[bytes]:
        yield b"abc"

    class NonConsumingStore:
        async def put_stream(
            self,
            stored_key: str,
            _data: Any,
            **_kwargs: Any,
        ) -> StoredArtifact:
            return StoredArtifact(key=stored_key, uri=f"memory://{stored_key}", size=0)

        async def delete(self, stored_key: str) -> None:
            deleted.append(stored_key)

    view = execution_phase._EngineArtifactStoreView(NonConsumingStore(), namespace)
    with pytest.raises(RuntimeError, match="complete artifact stream"):
        await view.put_stream(
            key,
            chunks(),
            content_type="application/octet-stream",
            max_bytes=8,
        )
    assert deleted == [f"{namespace}/artifact-0000"]

    with pytest.raises(ValueError, match="positive integer"):
        await execution_phase._EngineArtifactStoreView(NonConsumingStore(), namespace).put_stream(
            key,
            chunks(),
            content_type="application/octet-stream",
            max_bytes=0,
        )


async def test_engine_artifact_stream_success_records_exact_consumed_size() -> None:
    namespace = engine_artifact_namespace("runtime-coverage-stream-success")
    key = f"{namespace}/stream.bin"

    async def chunks() -> AsyncIterator[bytes]:
        yield b"abc"
        yield b"de"

    class ConsumingStore:
        async def put_stream(
            self,
            stored_key: str,
            data: AsyncIterator[bytes],
            **_kwargs: Any,
        ) -> StoredArtifact:
            payload = b"".join([chunk async for chunk in data])
            return StoredArtifact(
                key=stored_key,
                uri=f"memory://{stored_key}",
                size=len(payload),
            )

        async def delete(self, _stored_key: str) -> None:
            return None

    view = execution_phase._EngineArtifactStoreView(ConsumingStore(), namespace)
    stored = await view.put_stream(
        key,
        chunks(),
        content_type="application/octet-stream",
        max_bytes=10,
    )
    assert stored.size == 5
    assert stored.key == f"{namespace}/artifact-0000"
    assert view.attempted == {stored.key: 5}
    assert view.written[stored.key][1] == "application/octet-stream"


async def test_engine_artifact_stream_rejects_bad_chunks_and_overflow() -> None:
    namespace = engine_artifact_namespace("runtime-coverage-stream-bad-chunks")
    deleted: list[str] = []

    class ConsumingStore:
        async def put_stream(
            self,
            stored_key: str,
            data: AsyncIterator[bytes],
            **_kwargs: Any,
        ) -> StoredArtifact:
            payload = b"".join([chunk async for chunk in data])
            return StoredArtifact(key=stored_key, uri=f"memory://{stored_key}", size=len(payload))

        async def delete(self, stored_key: str) -> None:
            deleted.append(stored_key)

    async def wrong_type() -> AsyncIterator[bytes]:
        yield bytearray(b"abc")  # type: ignore[misc]

    async def too_large() -> AsyncIterator[bytes]:
        yield b"12345"

    for index, stream in enumerate((wrong_type(), too_large())):
        view = execution_phase._EngineArtifactStoreView(ConsumingStore(), namespace)
        message = "yield bytes" if index == 0 else "exceeds max_bytes"
        with pytest.raises(ValueError, match=message):
            await view.put_stream(
                f"{namespace}/stream-{index}.bin",
                stream,
                content_type="application/octet-stream",
                max_bytes=4,
            )
    assert deleted == [f"{namespace}/artifact-0000"] * 2


async def test_engine_artifact_cleanup_fails_closed_without_delete_contract() -> None:
    namespace = engine_artifact_namespace("runtime-coverage-cleanup")
    view = execution_phase._EngineArtifactStoreView(object(), namespace)
    with pytest.raises(RuntimeError, match="cannot compensate"):
        await view.cleanup_except(set())

    # A second call after explicit compensation has no provider work left.
    view._compensated.update(view._all_slot_keys())
    await view.cleanup_except(set())


async def test_engine_artifact_cleanup_tolerates_missing_slots_but_reports_other_failures() -> None:
    namespace = engine_artifact_namespace("runtime-coverage-cleanup-errors")
    deleted: list[str] = []

    class Store:
        async def delete(self, key: str) -> None:
            deleted.append(key)
            if key.endswith("0000"):
                raise KeyError(key)
            if key.endswith("0001"):
                raise OSError("provider unavailable")

    view = execution_phase._EngineArtifactStoreView(Store(), namespace)
    with pytest.raises(RuntimeError, match="could not compensate"):
        await view._cleanup_keys(
            (
                f"{namespace}/artifact-0000",
                f"{namespace}/artifact-0001",
                f"{namespace}/artifact-0002",
            )
        )
    assert deleted == [
        f"{namespace}/artifact-0000",
        f"{namespace}/artifact-0001",
        f"{namespace}/artifact-0002",
    ]
    assert f"{namespace}/artifact-0002" in view._compensated


async def test_owned_adapter_releases_leaf_and_resolver() -> None:
    closed: list[str] = []

    class Adapter:
        async def aclose(self) -> None:
            closed.append("adapter")

        async def ping(self) -> str:
            return "pong"

    class Resolver:
        async def close(self) -> None:
            closed.append("resolver")

    owned = execution_phase._own_resolution(Adapter(), Resolver())
    assert await owned.ping() == "pong"
    await owned.aclose()
    assert set(closed) == {"adapter", "resolver"}

    unchanged = object()
    assert execution_phase._own_resolution(unchanged, object()) is unchanged


async def test_owned_resolution_propagates_close_failure_after_closing_every_owner() -> None:
    closed: list[str] = []

    class Adapter:
        async def aclose(self) -> None:
            closed.append("adapter")
            raise OSError("adapter close failed")

    class Resolver:
        async def close(self) -> None:
            closed.append("resolver")

    with pytest.raises(OSError, match="adapter close failed"):
        await execution_phase._close_owned_resolution(Adapter(), Resolver())
    assert set(closed) == {"adapter", "resolver"}

    # A resolver without an explicit close method is still a valid legacy seam.
    await execution_phase._close_owned_resolution(None, object())


def test_execution_checkpoint_affinity_rejects_malformed_generations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = EngineHandle(
        engine="sim",
        connection_id="engine-connection",
        idempotency_key="runtime-coverage-affinity",
    )

    monkeypatch.setattr(
        execution_phase,
        "get_settings",
        lambda: SimpleNamespace(is_locked_down=False),
    )
    assert execution_phase._connection_reservation_affinity({}, handle) == (None, None)

    base = {
        "engine_connection_affinity_staged": True,
        "engine_connection_id": "engine-connection",
        "engine_connection_persisted": True,
    }
    with pytest.raises(RuntimeError, match="no checkpointed version"):
        execution_phase._connection_reservation_affinity(base, handle)
    with pytest.raises(RuntimeError, match="malformed"):
        execution_phase._connection_reservation_affinity(
            {**base, "engine_connection_version": "not-a-time"},
            handle,
        )
    with pytest.raises(RuntimeError, match="no timezone"):
        execution_phase._connection_reservation_affinity(
            {**base, "engine_connection_version": "2026-01-01T00:00:00"},
            handle,
        )

    artifact_base = {"artifact_store_connection_persisted": True}
    with pytest.raises(RuntimeError, match="no checkpointed version"):
        execution_phase._artifact_reservation_affinity(artifact_base, "artifact-store")
    with pytest.raises(RuntimeError, match="malformed"):
        execution_phase._artifact_reservation_affinity(
            {**artifact_base, "artifact_store_connection_version": "not-a-time"},
            "artifact-store",
        )
    with pytest.raises(RuntimeError, match="no timezone"):
        execution_phase._artifact_reservation_affinity(
            {
                **artifact_base,
                "artifact_store_connection_version": "2026-01-01T00:00:00",
            },
            "artifact-store",
        )


def test_execution_checkpoint_scalar_and_option_boundaries() -> None:
    assert execution_phase._entry({}) == {}
    assert execution_phase._checkpoint_int(None, label="count", default=7) == 7
    assert execution_phase._checkpoint_flag({}, "flag") is False
    with pytest.raises(ValueError, match="phase results"):
        execution_phase._entry(cast(PipelineState, {"phase_results": []}))
    with pytest.raises(ValueError, match="execution phase"):
        execution_phase._entry(cast(PipelineState, {"phase_results": {Phase.EXECUTION.value: []}}))
    with pytest.raises(ValueError, match="count"):
        execution_phase._checkpoint_int(True, label="count")
    with pytest.raises(ValueError, match="flag"):
        execution_phase._checkpoint_flag({"flag": "false"}, "flag")

    aware = "2026-01-01T00:00:00+00:00"
    assert execution_phase._checkpoint_timestamp_or_now(aware) == aware
    assert execution_phase._checkpoint_timestamp_or_now("not-a-time") != "not-a-time"
    assert execution_phase._checkpoint_timestamp_or_now("2026-01-01T00:00:00") != (
        "2026-01-01T00:00:00"
    )
    assert execution_phase._checkpoint_diagnostic_text(None, default="fallback") == "fallback"
    assert execution_phase._checkpoint_diagnostic_text("ordinary", default="fallback") == (
        "ordinary"
    )
    assert execution_phase._safe_connection_id(" connection ") is False

    assert execution_phase._engine_options(
        {
            "engine_options": {
                "abortive_stop": None,
                "test_id": "trusted-test",
                "test_instance_id": 7,
            }
        },
        "loadrunner",
    ) == {
        "abortive_stop": None,
        "test_id": "trusted-test",
        "test_instance_id": 7,
    }
    assert execution_phase._engine_options({"engine_options": {"fail_at_pct": 0.5}}, "sim") == {
        "fail_at_pct": 0.5
    }
    for options, message in (
        ([], "options are invalid"),
        ({"bad\x00key": 1}, "options are invalid"),
        ({"fail_at_pct": float("inf")}, "options are invalid"),
        ({"providerToken": "secret"}, "credential material"),
    ):
        with pytest.raises(ValueError, match=message):
            execution_phase._engine_options({"engine_options": options}, "sim")


def test_execution_thread_and_affinity_flags_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for config in (None, [], {"configurable": []}, {"configurable": {"thread_id": " bad "}}):
        with pytest.raises(ValueError):
            execution_phase._thread_id(config)  # type: ignore[arg-type]

    handle = EngineHandle(
        engine="sim",
        connection_id="engine-connection",
        idempotency_key="runtime-affinity-flags-execution-a1",
    )
    monkeypatch.setattr(
        execution_phase,
        "get_settings",
        lambda: SimpleNamespace(is_locked_down=False),
    )
    for entry, message in (
        ({"engine_connection_affinity_staged": "true"}, "affinity flag"),
        (
            {
                "engine_connection_affinity_staged": True,
                "engine_connection_id": "other-connection",
            },
            "does not match",
        ),
        (
            {
                "engine_connection_affinity_staged": True,
                "engine_connection_id": "engine-connection",
                "engine_connection_persisted": "true",
            },
            "persistence flag",
        ),
        (
            {
                "engine_connection_affinity_staged": True,
                "engine_connection_id": "engine-connection",
                "engine_connection_version": "2026-01-01T00:00:00+00:00",
            },
            "inconsistent",
        ),
    ):
        with pytest.raises(RuntimeError, match=message):
            execution_phase._connection_reservation_affinity(entry, handle)

    assert execution_phase._connection_reservation_affinity(
        {
            "engine_connection_affinity_staged": True,
            "engine_connection_id": "engine-connection",
        },
        handle,
    ) == ("engine-connection", None)

    for entry, connection_id, message in (
        ({}, " bad ", "connection id"),
        ({"artifact_store_connection_persisted": "true"}, "artifact-store", "flag"),
        (
            {"artifact_store_connection_version": "2026-01-01T00:00:00+00:00"},
            "artifact-store",
            "inconsistent",
        ),
    ):
        with pytest.raises(RuntimeError, match=message):
            execution_phase._artifact_reservation_affinity(entry, connection_id)


def test_provider_payload_validation_covers_success_and_rejection_contracts() -> None:
    phase = execution_phase.EngineRunPhase.RUNNING
    status = execution_phase._validated_engine_status(
        {
            "phase": phase,
            "progress_pct": 25,
            "live_stats": {"vusers": 2, "tps": 1.5},
            "message": "running",
        }
    )
    assert status.phase is phase
    assert status.live_stats is not None and status.live_stats.vusers == 2

    invalid_statuses = (
        {"phase": "not-a-phase"},
        {"phase": phase, "live_stats": {"tps": "fast"}},
        {"phase": phase, "message": "Authorization: Bearer status-secret"},
    )
    for value in invalid_statuses:
        with pytest.raises(ValueError, match="engine status"):
            execution_phase._validated_engine_status(value)

    summary = execution_phase._validated_engine_summary(
        {
            "engine": "sim",
            "passed": True,
            "kpis": {"p95_ms": 123.0},
            "sla_breaches": [],
        },
        expected_engine="sim",
    )
    assert summary.passed is True
    invalid_summaries = (
        {"engine": 7, "passed": True},
        {"engine": "sim", "passed": True, "kpis": {"p95": object()}},
        {"engine": "sim", "passed": True, "sla_breaches": [7]},
    )
    for value in invalid_summaries:
        with pytest.raises(ValueError, match="engine summary"):
            execution_phase._validated_engine_summary(value)
    with pytest.raises(ValueError, match="does not match"):
        execution_phase._validated_engine_summary(
            {"engine": "sim", "passed": True},
            expected_engine="loadrunner",
        )

    report = execution_phase._validated_engine_report({"ok": False, "issues": ["warning"]})
    assert report.ok is False
    for value in (
        {"ok": "yes"},
        {"issues": [7]},
        {"issues": ["password=report-secret"]},
    ):
        with pytest.raises(ValueError, match="validation report"):
            execution_phase._validated_engine_report(value)

    unsafe = execution_phase._validated_engine_handle(
        {
            "engine": "sim",
            "idempotency_key": "runtime-unsafe-handle-execution-a1",
            "external_run_id": "Authorization: Bearer provider-secret",
        },
        allow_credential_material=True,
    )
    assert unsafe.external_run_id is not None
    with pytest.raises(ValueError, match="engine handle"):
        execution_phase._validated_engine_handle(
            {"engine": "sim", "idempotency_key": "safe", "extras": {"value": object()}}
        )


def test_exact_provider_mapping_and_environment_stamp_validation() -> None:
    assert execution_phase._bounded_provider_mapping(
        {"safe": 1},
        max_items=1,
        max_key_chars=8,
        label="provider mapping",
    ) == {"safe": 1}
    for value in ([], {"": 1}, {"a": 1, "b": 2}):
        with pytest.raises(ValueError, match="provider mapping"):
            execution_phase._bounded_provider_mapping(
                value,
                max_items=1,
                max_key_chars=8,
                label="provider mapping",
            )

    for value in ({"unknown": 1}, {}):
        with pytest.raises(ValueError, match="provider payload"):
            execution_phase._exact_provider_payload(
                value,
                model_type=execution_phase.EngineRunStatus,
                allowed=frozenset({"phase"}),
                required=frozenset({"phase"}),
                label="provider payload",
            )

    cfg = PipelineConfigurable(
        project_id="project-1",
        app_id="app-1",
        environment_target="https://target.example.com",
        environment_target_version=3,
    )
    assert (
        execution_phase._verified_stamped_target(
            cfg,
            "https://target.example.com",
            3,
            current_app_id="app-1",
        )
        == "https://target.example.com"
    )
    for changed_cfg, target, version, app_id, message in (
        (PipelineConfigurable(), "https://target.example.com", 3, "app-1", "not authorized"),
        (cfg, "https://target.example.com", 3, "app-2", "application scope"),
        (cfg, "https://changed.example.com", 3, "app-1", "changed"),
        (cfg, "https://target.example.com", 4, "app-1", "changed"),
    ):
        with pytest.raises(ValueError, match=message):
            execution_phase._verified_stamped_target(
                changed_cfg,
                target,
                version,
                current_app_id=app_id,
            )


def test_engine_artifact_reference_ownership_is_exact() -> None:
    handle = EngineHandle(
        engine="sim",
        idempotency_key="runtime-artifact-ownership-execution-a1",
    )
    namespace = engine_artifact_namespace(handle.idempotency_key)
    key = f"{namespace}/artifact-0000"
    uri = execution_phase.canonical_artifact_uri(key)
    ref = {
        "kind": "engine_results",
        "name": "results.json",
        "uri": uri,
        "key": key,
        "media_type": "application/json",
    }
    stored = StoredArtifact(key=key, uri=uri, size=2)

    with pytest.raises(ValueError, match="must be a list"):
        execution_phase._validated_engine_artifacts(cast(Any, (ref,)), handle)
    with pytest.raises(ValueError, match="duplicates"):
        execution_phase._validated_engine_artifacts([ref, ref], handle)
    with pytest.raises(ValueError, match="not written"):
        execution_phase._validated_engine_artifacts([ref], handle, written={})
    with pytest.raises(ValueError, match="URI does not match its object"):
        execution_phase._validated_engine_artifacts(
            [ref],
            handle,
            written={
                key: (
                    StoredArtifact(key=key, uri="memory://different-object", size=2),
                    "application/json",
                )
            },
        )
    with pytest.raises(ValueError, match="media type"):
        execution_phase._validated_engine_artifacts(
            [ref],
            handle,
            written={key: (stored, "application/octet-stream")},
        )
    assert (
        execution_phase._validated_engine_artifacts(
            [ref],
            handle,
            written={key: (stored, "application/json")},
        )[0]["key"]
        == key
    )


def test_checkpointed_load_spec_and_builder_reject_malformed_replay() -> None:
    base = {
        "idempotency_key": "runtime-spec-execution-a1",
        "title": "runtime spec",
        "vusers": 1,
        "ramp_s": 0,
        "duration_s": 1,
    }
    assert execution_phase._validated_checkpoint_spec(base).title == "runtime spec"
    invalid_specs = (
        {**base, "unknown": 1},
        {**base, "ramp_s": 1 << 300},
        {**base, "slas": {"": 1}},
        {**base, "slas": {"p95": float("inf")}},
    )
    for value in invalid_specs:
        with pytest.raises(ValueError, match="load-test spec"):
            execution_phase._validated_checkpoint_spec(value)

    config = cast(
        RunnableConfig,
        {"configurable": {"thread_id": "runtime-spec-builder"}},
    )
    spec, options = execution_phase._build_spec(
        {},
        config,
        2,
        "sim",
    )
    assert spec.idempotency_key == "runtime-spec-builder-execution-a2"
    assert options == {}
    for state, message in (
        ({"phase_results": []}, "phase results"),
        (
            {"phase_results": {Phase.SCRIPT_SCENARIO.value: []}},
            "script-scenario",
        ),
        (
            {"phase_results": {Phase.SCRIPT_SCENARIO.value: {"load_test_spec": []}}},
            "load-test spec",
        ),
        ({"title": 7}, "pipeline title"),
    ):
        with pytest.raises(ValueError, match=message):
            execution_phase._build_spec(
                cast(PipelineState, state),
                config,
                1,
                "sim",
            )


def test_execution_handle_elapsed_and_recursion_helpers() -> None:
    limits = execution_phase.Limits(poll_timeout_s=10, poll_interval_s=2)
    assert execution_phase.recommended_recursion_limit(limits) > 0
    assert execution_phase.execution_idempotency_key("thread", 3) == "thread-execution-a3"

    handle = EngineHandle(
        engine="sim",
        idempotency_key="runtime-handle-execution-a1",
    )
    entry = {"engine_handle": handle.model_dump(mode="json")}
    assert execution_phase._handle_from({}, entry) == handle
    assert (
        execution_phase._handle_from(
            {"engine_handle": handle.model_dump(mode="json")},
            {},
        )
        == handle
    )
    with pytest.raises(ValueError, match="missing"):
        execution_phase._handle_from({}, {})
    assert execution_phase._elapsed_s("not-a-time") is None


def test_execution_retry_interrupts_preserve_exact_attempt_and_retry_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded provider retry windows reopen without advancing the attempt."""

    interrupts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        execution_phase,
        "interrupt",
        lambda payload: interrupts.append(payload),
    )
    config = cast(
        RunnableConfig,
        {"configurable": {"thread_id": "runtime-retry-thread"}},
    )

    provision_state = cast(
        PipelineState,
        {
            "phase_results": {
                Phase.EXECUTION.value: {
                    "attempt": 4,
                    "engine_provision_last_error": "provider unavailable",
                }
            }
        },
    )
    blocked = execution_phase.engine_provision_blocked(provision_state, config)
    resumed = execution_phase.engine_provision_resume(provision_state, config)
    assert blocked.goto == resumed.goto == "engine_provision"
    assert blocked.update is not None and resumed.update is not None
    assert blocked.update["phase_results"][Phase.EXECUTION.value]["attempt"] == 4
    assert interrupts[-1]["kind"] == "engine_provision_retry"

    settle_state = cast(
        PipelineState,
        {
            "phase_results": {
                Phase.EXECUTION.value: {
                    "attempt": 5,
                    "engine_collection_staged": True,
                    "engine_collection_settle_last_error": "teardown unavailable",
                }
            }
        },
    )
    settle_blocked = execution_phase.engine_collection_settle_blocked(settle_state, config)
    settle_resumed = execution_phase.engine_collection_settle_resume(settle_state, config)
    assert settle_blocked.goto == settle_resumed.goto == "engine_collection_settle"
    assert interrupts[-1]["kind"] == "engine_collection_settle_retry"

    unstaged = cast(
        PipelineState,
        {"phase_results": {Phase.EXECUTION.value: {"attempt": 5}}},
    )
    with pytest.raises(RuntimeError, match="requires staged collection"):
        execution_phase.engine_collection_settle_resume(unstaged, config)


@pytest.mark.parametrize(
    ("entry_update", "expected"),
    [
        ({"engine_cleanup_required": True}, "engine_cleanup"),
        ({"engine_collection_settle_required": True}, "engine_collection_settle_resume"),
        ({"engine_collection_staged": True}, "engine_collection_settle"),
        (
            {
                "engine_collection_settled": True,
                "engine_collection_next": "open_output_gate",
            },
            "open_output_gate",
        ),
        ({"engine_collection_index_required": True}, "engine_collection_resume"),
        ({"engine_collection_required": True}, "engine_collection_resume"),
        ({"engine_provision_required": True}, "engine_provision_resume"),
        ({}, "prepare"),
    ],
)
def test_execution_recovery_router_prioritizes_checkpointed_intents(
    entry_update: dict[str, Any],
    expected: str,
) -> None:
    state = cast(
        PipelineState,
        {
            "phase_results": {
                Phase.EXECUTION.value: {"attempt": 1, **entry_update},
            }
        },
    )
    assert execution_phase.route_execution_entry(state) == expected


def test_execution_recovery_router_rejects_settled_state_without_destination() -> None:
    state = cast(
        PipelineState,
        {
            "phase_results": {
                Phase.EXECUTION.value: {
                    "attempt": 1,
                    "engine_collection_settled": True,
                }
            }
        },
    )
    with pytest.raises(RuntimeError, match="no valid continuation"):
        execution_phase.route_execution_entry(state)


@pytest.mark.parametrize("indexing", [False, True])
def test_execution_collection_retry_resumes_exact_stage(
    monkeypatch: pytest.MonkeyPatch,
    indexing: bool,
) -> None:
    interrupts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        execution_phase,
        "interrupt",
        lambda payload: interrupts.append(payload),
    )
    entry = {
        "attempt": 6,
        "engine_collection_index_required": indexing,
        (
            "engine_collection_index_last_error" if indexing else "engine_collection_last_error"
        ): "temporary store failure",
    }
    state = cast(
        PipelineState,
        {"phase_results": {Phase.EXECUTION.value: entry}},
    )
    config = cast(
        RunnableConfig,
        {"configurable": {"thread_id": "runtime-collection-retry"}},
    )

    blocked = execution_phase.engine_collection_blocked(state, config)
    resumed = execution_phase.engine_collection_resume(state, config)
    expected = "engine_collection_index" if indexing else "engine_collect"
    assert blocked.goto == resumed.goto == expected
    assert interrupts[-1]["kind"] == "engine_collection_retry"
    assert interrupts[-1]["attempt"] == 6
