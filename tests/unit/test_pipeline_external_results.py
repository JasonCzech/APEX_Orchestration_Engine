"""External-results analysis runs + the LLM-agent dispatch/compose helpers.

The graph runs are driven through an InMemorySaver-compiled graph (offline, stub
backend). They prove that externally-supplied results let an analysis-only run
(reporting/postmortem) satisfy the execution prerequisite honestly.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

import apex.graphs.pipeline.phase_subgraph as ps
from apex.domain.pipeline import Phase
from apex.graphs.pipeline.graph import builder
from apex.graphs.pipeline.state import PipelineState

AUTO = {"prompt_review": "auto", "output_review": "auto"}

EXTERNAL = {
    "source": "results-dashboard",
    "uri": "https://results.example.com/run/42",
    "engine": "jmeter",
    "passed": True,
    "kpis": {"p95_ms": 450.0, "error_rate": 0.01},
    "summary": "Baseline run completed",
}


def compiled() -> CompiledStateGraph[Any, Any, Any, Any]:
    return builder.compile(checkpointer=InMemorySaver())


def config(thread_id: str, **configurable: Any) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id, **configurable}}


def test_external_results_enable_analysis_only_run() -> None:
    g = compiled()
    cfg = config(
        "ext1",
        phases=["reporting", "postmortem"],
        gates={"reporting": dict(AUTO), "postmortem": dict(AUTO)},
    )
    result = g.invoke(
        {"title": "Analyze", "request": "Summarize and recommend", "external_results": EXTERNAL},
        cfg,
    )

    assert "__interrupt__" not in result
    assert result["phases_plan"] == ["reporting", "postmortem"]

    execution = result["phase_results"]["execution"]
    assert execution["status"] == "succeeded"
    assert execution["external"] is True
    assert execution["attempt"] == 1
    assert execution["test_summary"]["kpis"]["p95_ms"] == 450.0
    assert result["external_results"] is None

    for phase in ("reporting", "postmortem"):
        assert result["phase_results"][phase]["status"] == "succeeded"

    # The reporting agent sees the externally-supplied KPIs.
    assert "execution passed on engine jmeter" in result["phase_results"]["reporting"]["summary"]
    # The source link is seeded as a context packet for the agent/dashboard.
    assert any(packet["id"] == "external-results" for packet in result["context_packets"])


def test_new_external_results_advance_attempt_and_replace_prior_evidence() -> None:
    g = compiled()
    cfg = config("ext-rerun", phases=["reporting"], gates={"reporting": dict(AUTO)})
    first = g.invoke(
        {"title": "Analyze", "request": "first", "external_results": EXTERNAL},
        cfg,
    )
    second_external = {
        **EXTERNAL,
        "source": "second-dashboard",
        "summary": "Second run completed",
        "kpis": {"p95_ms": 275.0},
    }

    second = g.invoke(
        {"title": "Analyze", "request": "second", "external_results": second_external},
        cfg,
    )

    assert first["phase_results"]["execution"]["attempt"] == 1
    assert second["phase_results"]["execution"]["attempt"] == 2
    assert second["phase_results"]["execution"]["test_summary"]["kpis"]["p95_ms"] == 275.0
    packets = [p for p in second["context_packets"] if p["id"] == "external-results"]
    assert len(packets) == 1
    assert packets[0]["source"] == "second-dashboard"
    assert second["external_results"] is None


def test_reporting_only_without_external_results_still_raises() -> None:
    g = compiled()
    cfg = config("ext2", phases=["reporting"], gates={"reporting": dict(AUTO)})
    with pytest.raises(ValueError, match="execution"):
        g.invoke({"title": "Analyze", "request": "x"}, cfg)


def test_graph_revalidation_rejects_signed_external_results_uri() -> None:
    g = compiled()
    cfg = config("ext-signed", phases=["reporting"], gates={"reporting": dict(AUTO)})

    with pytest.raises(ValueError, match="external results uri") as raised:
        g.invoke(
            {
                "title": "Analyze",
                "request": "summarize",
                "external_results": {
                    **EXTERNAL,
                    "uri": (
                        "https://results.example.com/run/42?X-Amz-Signature=graph-signed-url-secret"
                    ),
                },
            },
            cfg,
        )

    assert "graph-signed-url-secret" not in str(raised.value)


def test_anthropic_backend_without_key_degrades_to_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ps, "get_settings", lambda: SimpleNamespace(llm=SimpleNamespace(anthropic_api_key=None))
    )
    agent = ps._make_agent(Phase.REPORTING)
    state: PipelineState = {
        "title": "X",
        "request": "Y",
        "phase_results": {"reporting": {"attempt": 1}},
    }
    update = agent(state, config("t", agent_backend="anthropic"))
    entry = update["phase_results"]["reporting"]
    assert "stub result" in entry["summary"]
    assert any("no Anthropic API key" in warning for warning in entry["warnings"])


def test_default_backend_uses_stub() -> None:
    agent = ps._make_agent(Phase.STORY_ANALYSIS)
    state: PipelineState = {
        "title": "X",
        "request": "Y",
        "phase_results": {"story_analysis": {"attempt": 1}},
    }
    update = agent(state, config("t"))  # no agent_backend -> "stub"
    entry = update["phase_results"]["story_analysis"]
    assert "stub result" in entry["summary"]
    assert "warnings" not in entry


def test_message_text_handles_string_and_blocks() -> None:
    assert ps._message_text(SimpleNamespace(content="hello  ")) == "hello"
    blocks = [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "answer"}]
    assert ps._message_text(SimpleNamespace(content=blocks)) == "answer"


def test_compose_system_layers_application_context() -> None:
    system = ps._compose_system({"system": "sys", "application": "app constraints"})
    assert "sys" in system
    assert "APPLICATION CONTEXT" in system
    assert "app constraints" in system


def test_compose_user_includes_packets_and_external_kpis() -> None:
    state: PipelineState = {
        "context_packets": [
            {"id": "p1", "source": "s", "title": "Run meta", "summary": "ran ok", "ref": "u"}
        ],
        "phase_results": {
            "execution": {"test_summary": {"engine": "jmeter", "passed": True, "kpis": {"tps": 12}}}
        },
    }
    resolved = {"system": "sys", "user": "analyze the results", "application": None}
    out = ps._compose_user(state, Phase.REPORTING, resolved, {})
    assert "analyze the results" in out
    assert "CONTEXT / EVIDENCE" in out
    assert "Run meta: ran ok" in out
    assert "Execution results:" in out


def test_context_packets_block_fences_document_text() -> None:
    state: PipelineState = {
        "context_packets": [
            {
                "id": "document-d1",
                "source": "document",
                "title": "Spec",
                "summary": "the spec",
                "ref": "/v1/artifacts/k",
                "text": "Story: user signs in.",
            }
        ]
    }
    block = ps._context_packets_block(state)
    assert "CONTEXT / EVIDENCE" in block
    assert "- Spec: the spec (/v1/artifacts/k)" in block
    assert '"""' in block
    assert "Story: user signs in." in block


def test_context_packets_block_truncates_to_total_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ps,
        "get_settings",
        lambda: SimpleNamespace(documents=SimpleNamespace(max_context_chars_total=90)),
    )
    state: PipelineState = {
        "context_packets": [
            {"id": "d1", "source": "document", "title": "A", "text": "X" * 25},
            {"id": "d2", "source": "document", "title": "B", "text": "Y" * 25},
        ]
    }
    block = ps._context_packets_block(state)
    # The complete rendered block (headers, fences, metadata, and text) shares one
    # budget; the second packet is truncated after the first consumes its share.
    assert len(block) <= 90
    assert "X" * 25 in block
    assert "…[truncated]" in block
    assert "Y" in block
    assert "Y" * 2 not in block


def test_compose_user_injects_document_text_for_story_analysis() -> None:
    state: PipelineState = {
        "context_packets": [
            {"id": "document-d1", "source": "document", "title": "Spec", "text": "Login via SSO."}
        ]
    }
    resolved = {"system": "sys", "user": "Analyze this story", "application": None}
    out = ps._compose_user(state, Phase.STORY_ANALYSIS, resolved, {})
    assert "Analyze this story" in out
    assert "Login via SSO." in out
    assert "CONTEXT / EVIDENCE" in out


def test_accumulate_usage_sums_tokens_and_details() -> None:
    acc: dict[str, Any] = {}
    ps._accumulate_usage(
        acc,
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_token_details": {"cache_read": 2},
        },
    )
    ps._accumulate_usage(acc, {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4})
    assert acc["input_tokens"] == 13
    assert acc["output_tokens"] == 6
    assert acc["total_tokens"] == 19
    assert acc["input_token_details"]["cache_read"] == 2


def test_accumulate_usage_sanitizes_malformed_provider_counts() -> None:
    acc: dict[str, Any] = {
        "input_tokens": "already-malformed",
        "input_token_details": "not-a-mapping",
    }

    ps._accumulate_usage(
        acc,
        {
            "input_tokens": "not-a-number",
            "output_tokens": float("inf"),
            "total_tokens": -100,
            "input_token_details": {
                "cache_read": float("nan"),
                "negative": -5,
                123: 9,
            },
        },
    )

    assert acc["input_tokens"] == 0
    assert acc["output_tokens"] == 0
    assert acc["total_tokens"] == 0
    assert acc["input_token_details"] == {"cache_read": 0, "negative": 0}


def test_accumulate_usage_saturates_repeated_tool_loop_totals() -> None:
    from apex.services.pricing import MAX_TOKEN_COUNT

    acc: dict[str, Any] = {}
    provider_usage = {
        "input_tokens": MAX_TOKEN_COUNT - 1,
        "output_tokens": MAX_TOKEN_COUNT - 1,
        "total_tokens": MAX_TOKEN_COUNT - 1,
        "output_token_details": {"reasoning": MAX_TOKEN_COUNT - 1},
    }

    ps._accumulate_usage(acc, provider_usage)
    ps._accumulate_usage(acc, provider_usage)

    assert acc["input_tokens"] == MAX_TOKEN_COUNT
    assert acc["output_tokens"] == MAX_TOKEN_COUNT
    assert acc["total_tokens"] == MAX_TOKEN_COUNT
    assert acc["output_token_details"]["reasoning"] == MAX_TOKEN_COUNT
