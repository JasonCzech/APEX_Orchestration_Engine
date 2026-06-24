"""Context graph: deterministic stub-evidence gathering (no LLM until M4)."""

import pytest

from apex.graphs.context.graph import ContextState, graph
from apex.services.connections import ConnectionResolver

INPUT = ContextState(
    subject="Checkout latency regression",
    work_item_keys=["PHX-241", "NOPE-1"],
    document_ids=["doc-checkout-runbook", "missing-doc"],
)


@pytest.fixture(autouse=True)
def static_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin a store-less resolver (DEV_CONNECTIONS stubs only) so tests never touch
    Postgres regardless of what the process-wide resolver is configured with."""
    resolver = ConnectionResolver()
    monkeypatch.setattr("apex.graphs.context.graph.get_connection_resolver", lambda: resolver)


async def test_gathers_evidence_and_tolerates_unknown_refs() -> None:
    result = await graph.ainvoke(INPUT)
    evidence = result["evidence"]
    # One bad work-item key + one bad doc id are skipped, not fatal.
    assert [packet["source"] for packet in evidence] == ["work_tracking", "documents"]

    work = evidence[0]
    assert work["title"].startswith("Checkout latency p95 regression")
    assert work["ref"] == "https://tracker.stub.local/browse/PHX-241"
    assert work["summary"] and len(work["summary"]) <= 280
    assert work["id"]

    doc = evidence[1]
    assert doc["title"] == "Checkout service performance runbook"
    assert doc["ref"] == "stub://docs/checkout-runbook"


async def test_summary_is_deterministic_and_mentions_subject() -> None:
    first = await graph.ainvoke(INPUT)
    second = await graph.ainvoke(INPUT)
    assert first["summary"] == second["summary"]
    assert first["evidence"] == second["evidence"]  # content-derived packet ids
    lines = first["summary"].splitlines()
    assert lines[0] == "Context summary for: Checkout latency regression"
    assert any(line.startswith("- [work_tracking]") for line in lines)
    assert any(line.startswith("- [documents]") for line in lines)


async def test_empty_input_yields_empty_evidence() -> None:
    result = await graph.ainvoke(ContextState(subject="anything"))
    assert result["evidence"] == []
    assert "no evidence gathered" in result["summary"]


async def test_resolver_scope_errors_are_not_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    class ScopeFailingResolver:
        async def resolve(self, *args: object, **kwargs: object) -> object:
            raise ValueError("connection is scoped to project 'p1', not 'p2'")

    monkeypatch.setattr(
        "apex.graphs.context.graph.get_connection_resolver", lambda: ScopeFailingResolver()
    )
    with pytest.raises(ValueError, match="scoped to project"):
        await graph.ainvoke(
            ContextState(subject="anything", work_item_keys=["PHX-241"], project_id="p2")
        )
