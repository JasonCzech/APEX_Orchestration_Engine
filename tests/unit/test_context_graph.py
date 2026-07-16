"""Context graph: deterministic stub-evidence gathering (no LLM until M4)."""

import asyncio
import gc
import threading
from typing import Any, cast

import pytest

import apex.graphs.context.graph as context_graph
from apex.domain.integrations import WorkItem
from apex.graphs.context.graph import ContextState, graph
from apex.services.connections import ConnectionResolver

INPUT = ContextState(
    subject="Checkout latency regression",
    work_item_keys=["PHX-241", "NOPE-1"],
    document_packets=[
        {
            "id": "document-upload-1",
            "source": "document",
            "title": "Checkout service performance runbook",
            "ref": "/v1/artifacts/documents/upload-1/runbook.md",
            "text": "Use the checkout latency runbook during regressions.",
        }
    ],
)


@pytest.fixture(autouse=True)
def static_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin a store-less resolver (DEV_CONNECTIONS stubs only) so tests never touch
    Postgres regardless of what the process-wide resolver is configured with."""
    monkeypatch.setattr("apex.graphs.context.graph._make_resolver", ConnectionResolver)


async def test_gathers_evidence_and_tolerates_unknown_refs() -> None:
    result = await graph.ainvoke(INPUT)
    evidence = result["evidence"]
    # One bad work-item key is skipped; uploaded document evidence is already authorized.
    assert [packet["source"] for packet in evidence] == ["work_tracking", "document"]

    work = evidence[0]
    assert work["title"].startswith("Checkout latency p95 regression")
    assert work["ref"] == "https://tracker.stub.local/browse/PHX-241"
    assert work["summary"] and len(work["summary"]) <= 280
    assert work["id"]

    doc = evidence[1]
    assert doc["title"] == "Checkout service performance runbook"
    assert doc["ref"] == "/v1/artifacts/documents/upload-1/runbook.md"
    assert doc["id"] == "document-upload-1"


async def test_summary_is_deterministic_and_mentions_subject() -> None:
    first = await graph.ainvoke(INPUT)
    second = await graph.ainvoke(INPUT)
    assert first["summary"] == second["summary"]
    assert first["evidence"] == second["evidence"]  # content-derived packet ids
    lines = first["summary"].splitlines()
    assert lines[0] == "Context summary for: Checkout latency regression"
    assert any(line.startswith("- [work_tracking]") for line in lines)
    assert any(line.startswith("- [document]") for line in lines)


async def test_context_summary_is_available_on_the_public_custom_stream() -> None:
    events = [event async for event in graph.astream(INPUT, stream_mode="custom")]

    summaries = [event for event in events if event.get("type") == "context_summary"]
    assert len(summaries) == 1
    assert summaries[0]["schema_version"] == 1
    assert summaries[0]["summary"].startswith("Context summary for:")
    assert "evidence" not in summaries[0]


async def test_empty_input_yields_empty_evidence() -> None:
    result = await graph.ainvoke(ContextState(subject="anything"))
    assert result["evidence"] == []
    assert "no evidence gathered" in result["summary"]


async def test_resolver_scope_errors_are_not_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    class ScopeFailingResolver(ConnectionResolver):
        async def resolve(self, *args: object, **kwargs: object) -> object:
            raise ValueError("connection is scoped to project 'p1', not 'p2'")

    monkeypatch.setattr("apex.graphs.context.graph._make_resolver", ScopeFailingResolver)
    with pytest.raises(RuntimeError, match="evidence gathering failed"):
        await graph.ainvoke(
            ContextState(subject="anything", work_item_keys=["PHX-241"], project_id="p2")
        )


async def test_direct_graph_rejects_scoped_real_tracker_without_external_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[str] = []
    closed: set[str] = set()

    class Tracker:
        provider = "jira"
        project_id = None

        async def get_item(self, key: str) -> WorkItem:
            reads.append(key)
            raise AssertionError("scope validation must run before provider reads")

        async def aclose(self) -> None:
            closed.add("tracker")

    class Resolver:
        async def resolve(self, *_args: object, **_kwargs: object) -> Tracker:
            return Tracker()

        async def close(self) -> None:
            closed.add("resolver")

    monkeypatch.setattr(context_graph, "_make_resolver", Resolver)

    with pytest.raises(RuntimeError, match="evidence gathering failed") as raised:
        await graph.ainvoke(
            ContextState(
                subject="cross-project read",
                work_item_keys=["OTHER-1"],
                project_id="internal-p1",
            )
        )

    assert reads == []
    assert closed == {"tracker", "resolver"}
    assert raised.value.__cause__ is None


async def test_graph_provider_failure_is_redacted_and_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "opaque-context-provider-error-secret-canary"

    class FailingResolver(ConnectionResolver):
        async def resolve(self, *args: object, **kwargs: object) -> object:
            raise OSError(f"upstream exploded with opaque value {secret}")

    monkeypatch.setattr("apex.graphs.context.graph._make_resolver", FailingResolver)
    with pytest.raises(RuntimeError, match="evidence gathering failed") as raised:
        await graph.ainvoke(ContextState(subject="anything", work_item_keys=["PHX-241"]))

    assert secret not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


async def test_simultaneous_provider_failures_are_all_drained_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = {
        "ONE": "context-first-failure-secret-canary",
        "TWO": "context-second-failure-secret-canary",
    }
    both_started = asyncio.Event()
    release = asyncio.Event()
    started = 0

    class Tracker:
        async def get_item(self, key: str) -> WorkItem:
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await release.wait()
            raise RuntimeError(f"Authorization: Bearer {secrets[key]}")

    class Resolver:
        async def resolve(self, *_args: object, **_kwargs: object) -> Tracker:
            return Tracker()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(context_graph, "_make_resolver", Resolver)
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop_errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    task = asyncio.create_task(
        context_graph.gather_evidence(
            ContextState(subject="incident", work_item_keys=["ONE", "TWO"])
        )
    )
    try:
        await asyncio.wait_for(both_started.wait(), timeout=1)
        release.set()
        with pytest.raises(RuntimeError, match="evidence gathering failed") as raised:
            await task
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    rendered_error = repr(raised.value)
    assert all(secret not in rendered_error for secret in secrets.values())
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert loop_errors == []


async def test_resolver_operational_errors_are_not_reported_as_empty_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingResolver(ConnectionResolver):
        async def resolve(self, *args: object, **kwargs: object) -> object:
            raise OSError("connection store unavailable")

    monkeypatch.setattr("apex.graphs.context.graph._make_resolver", FailingResolver)
    with pytest.raises(OSError, match="connection store unavailable"):
        await context_graph._work_tracking_evidence(["PHX-241"], None)


async def test_provider_operational_errors_are_not_reported_as_missing_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Tracker:
        async def get_item(self, key: str) -> WorkItem:
            del key
            raise RuntimeError("provider unavailable")

    class Resolver:
        async def __aenter__(self) -> "Resolver":
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def resolve(self, *_args: object, **_kwargs: object) -> Tracker:
            return Tracker()

    monkeypatch.setattr(context_graph, "_make_resolver", Resolver)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await context_graph._work_tracking_evidence(["PHX-241"], None)


async def test_provider_parser_key_error_is_not_reported_as_missing_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Tracker:
        async def get_item(self, key: str) -> WorkItem:
            del key
            parsed: dict[str, WorkItem] = {}
            return parsed["missing-provider-field"]

    class Resolver:
        async def __aenter__(self) -> "Resolver":
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def resolve(self, *_args: object, **_kwargs: object) -> Tracker:
            return Tracker()

    monkeypatch.setattr(context_graph, "_make_resolver", Resolver)

    with pytest.raises(KeyError, match="missing-provider-field"):
        await context_graph._work_tracking_evidence(["PHX-241"], None)


async def test_provider_evidence_is_redacted_before_checkpoint_and_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "context-provider-secret-canary"

    class Tracker:
        async def get_item(self, key: str) -> WorkItem:
            return WorkItem(
                key=key,
                title=f"Authorization: Bearer {secret}",
                description=f"database_password={secret}" + ("x" * 10_000),
                url=f"https://tracker.test/{key}",
            )

    class Resolver:
        async def __aenter__(self) -> "Resolver":
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def resolve(self, *_args: object, **_kwargs: object) -> Tracker:
            return Tracker()

    monkeypatch.setattr(context_graph, "_make_resolver", Resolver)
    result = await graph.ainvoke(ContextState(subject="incident", work_item_keys=["SAFE-1"]))

    rendered = repr(result)
    assert secret not in rendered
    assert "[REDACTED]" in rendered
    packet = result["evidence"][0]
    assert len(packet["title"]) <= 500
    assert len(packet["ref"]) <= 2_048
    assert len(packet["summary"]) <= context_graph._SUMMARY_SNIPPET_CHARS
    assert secret not in result["summary"]


def test_packet_bounds_huge_provider_summary_before_stripping() -> None:
    secret = "huge-context-secret-canary"
    packet = context_graph._packet(
        "work_tracking",
        "incident",
        None,
        (" " * 1_000_000) + f"password={secret}",
    )

    assert packet["summary"] is None
    assert secret not in repr(packet)


async def test_graph_rejects_fanout_before_resolving_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_resolver() -> ConnectionResolver:
        raise AssertionError("provider resolution must happen after input validation")

    monkeypatch.setattr("apex.graphs.context.graph._make_resolver", forbidden_resolver)
    with pytest.raises(ValueError, match="work_item_keys exceeds"):
        await graph.ainvoke(
            ContextState(
                subject="incident",
                work_item_keys=[f"ITEM-{index}" for index in range(51)],
            )
        )


async def test_graph_input_schema_drops_caller_owned_output_fields() -> None:
    result = await graph.ainvoke(
        cast(
            Any,
            {
                "subject": "incident",
                "summary": "forged summary",
                "evidence": [{"id": "forged"}],
            },
        )
    )
    assert result["summary"] != "forged summary"
    assert result["evidence"] == []


async def test_work_tracking_deadline_is_not_reported_as_partial_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()

    class Tracker:
        async def get_item(self, key: str) -> WorkItem:
            if key == "FAST-1":
                return WorkItem(key=key, title="fast result")
            await release.wait()
            return WorkItem(key=key, title="late result")

    class Resolver:
        async def __aenter__(self) -> "Resolver":
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def resolve(self, *_args: object, **_kwargs: object) -> Tracker:
            return Tracker()

    monkeypatch.setattr(context_graph, "_make_resolver", Resolver)
    monkeypatch.setattr(context_graph, "CONTEXT_EVIDENCE_TOTAL_TIMEOUT_S", 0.02)

    with pytest.raises(TimeoutError, match="evidence gathering timed out"):
        await context_graph._work_tracking_evidence(["SLOW-1", "FAST-1", "SLOW-2"], None)


async def test_parent_cancellation_settles_all_evidence_children_before_resolver_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    exited_with_active: list[int] = []

    class Tracker:
        async def get_item(self, key: str) -> WorkItem:
            del key
            nonlocal active
            active += 1
            if active == 2:
                started.set()
            try:
                await release.wait()
                raise AssertionError("cancelled evidence call continued after parent cancellation")
            finally:
                active -= 1

    class Resolver:
        async def __aenter__(self) -> "Resolver":
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            exited_with_active.append(active)
            return False

        async def resolve(self, *_args: object, **_kwargs: object) -> Tracker:
            return Tracker()

    monkeypatch.setattr(context_graph, "_make_resolver", Resolver)
    task = asyncio.create_task(context_graph._work_tracking_evidence(["ONE", "TWO"], None))
    await asyncio.wait_for(started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert active == 0
    assert exited_with_active == [0]


async def test_repeated_parent_cancellation_does_not_abandon_child_settlement() -> None:
    child_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def child() -> None:
        child_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()

    child_task = asyncio.create_task(child())
    await child_started.wait()
    settlement = asyncio.create_task(context_graph._settle_cancelled_evidence_tasks([child_task]))
    await cleanup_started.wait()

    settlement.cancel()
    await asyncio.sleep(0)
    settlement.cancel()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await settlement
    assert child_task.done()
    assert child_task.cancelled()


async def test_tracker_and_resolver_closes_survive_repeated_cancellation() -> None:
    started: set[str] = set()
    both_started = asyncio.Event()
    release = asyncio.Event()
    closed: set[str] = set()

    async def close(name: str) -> None:
        started.add(name)
        if len(started) == 2:
            both_started.set()
        await release.wait()
        closed.add(name)

    class Tracker:
        async def aclose(self) -> None:
            await close("tracker")

    class Resolver:
        async def close(self) -> None:
            await close("resolver")

    task = asyncio.create_task(
        context_graph._close_context_resources_definitively(Tracker(), Resolver())
    )
    await both_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == {"tracker", "resolver"}


def test_context_provider_admission_is_shared_across_event_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Use the real evidence path from two thread-owned event loops. With one
    # process slot, distinct keys still execute one provider call at a time.
    admission = threading.BoundedSemaphore(1)
    monkeypatch.setattr(context_graph, "_CONTEXT_PROVIDER_ADMISSION", admission)
    start = threading.Barrier(3)
    lock = threading.Lock()
    active = 0
    maximum = 0

    class Tracker:
        async def get_item(self, key: str) -> WorkItem:
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            await asyncio.sleep(0.02)
            with lock:
                active -= 1
            return WorkItem(key=key, title=key)

    class Resolver:
        async def __aenter__(self) -> "Resolver":
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def resolve(self, *_args: object, **_kwargs: object) -> Tracker:
            return Tracker()

    monkeypatch.setattr(context_graph, "_make_resolver", Resolver)

    def worker(key: str) -> None:
        start.wait()
        asyncio.run(context_graph._work_tracking_evidence([key], None))

    threads = [threading.Thread(target=worker, args=(f"KEY-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert maximum == 1
