"""Analysis lifecycle regressions using the actual durable state reducer."""

import json
from typing import cast

import httpx
import pytest
from langchain_core.runnables import RunnableConfig

from apex.adapters.registry import PortKind
from apex.domain.pipeline import utcnow_iso
from apex.graphs.pipeline import analysis_phase as module
from apex.graphs.pipeline.state import PipelineState, merge_phase_results
from apex.ports.analysis import AnalysisCapabilities, AnalysisJob, AnalysisResult
from apex.ports.artifact_store import StoredArtifact


@pytest.fixture(autouse=True)
def isolated_native_receipts(monkeypatch):
    """Lifecycle unit fixtures isolate persistence; receipt tests exercise SQL."""

    async def claim(_scope):
        return None

    async def record(_scope, _job_id):
        return None

    monkeypatch.setattr(module, "claim_native_analysis", claim)
    monkeypatch.setattr(module, "record_native_analysis_receipt", record)


def fixture(*, native=False, count=1):
    config = {
        "configurable": {
            "thread_id": "analysis-bounds",
            "engine": "apex_load" if native else "loadrunner",
            "phases": ["reporting"],
            "analysis": {
                "selections": [
                    {
                        "tool": "internal_rca" if native else "other",
                        "connection_id": f"connection-{i}",
                    }
                    for i in range(count)
                ]
            },
        }
    }
    state = {
        "phase_results": {
            "reporting": {"attempt": 1},
            "execution": {
                "test_summary": {
                    "engine": "apex_load" if native else "loadrunner",
                    "kpis": {"p95_ms": 50},
                },
                "engine_handle": {"external_run_id": "native-run", "connection_id": "connection-0"},
            },
        }
    }
    typed_state = cast(PipelineState, state)
    typed_config = cast(RunnableConfig, config)
    apply(typed_state, module.prepare_analysis(typed_state, typed_config))
    return typed_state, typed_config


def apply(state, update):
    state["phase_results"] = merge_phase_results(
        state.get("phase_results", {}), update["phase_results"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("locked_prompt", [False, True])
async def test_maximum_connector_results_fit_real_phase_reducer(monkeypatch, locked_prompt):
    state, config = fixture(count=16)
    if locked_prompt:
        state.get("phase_results", {})["reporting"]["resolved_prompt"] = {"system_locked": True}
    submitted = {}
    persisted = []

    class Adapter:
        def __init__(self, connection_id):
            self.connection_id = connection_id

        async def capabilities(self):
            return AnalysisCapabilities(
                schema_version=1,
                engines=["loadrunner"],
                evidence_kinds=["test_summary"],
                available=True,
            )

        async def submit(self, request):
            submitted[self.connection_id] = request
            return AnalysisJob(job_id=self.connection_id, status="succeeded")

        async def status(self, job_id):
            return AnalysisJob(job_id=job_id, status="succeeded")

        async def result(self, job_id):
            return AnalysisResult(
                schema_version=1,
                summary="s" * 12000,
                tool="other",
                tool_version="1",
                input_digest=submitted[job_id]["input_digest"],
                findings=[{"claim": "f" * 12000}],
                warnings=["A complete warning"],
                gaps=["An evidence gap"],
            )

    class Resolver:
        async def resolve_with_connection_id(self, kind, **kwargs):
            if kind is PortKind.ANALYSIS:
                return Adapter(kwargs["connection_id"]), kwargs["connection_id"]
            return object(), "store"

        async def close(self):
            pass

    async def persist(_store, **kwargs):
        persisted.append(json.loads(kwargs["payload"]))
        return StoredArtifact(
            key=kwargs["artifact_key"], uri="memory://analysis", size=len(kwargs["payload"])
        )

    monkeypatch.setattr(module, "_make_analysis_resolver", Resolver)
    monkeypatch.setattr(module, "persist_artifact_with_intent", persist)
    for _ in range(16 * 3):
        apply(state, await module.analysis_step(state, config))
    assert module.route_analysis(state) == "report_agent"
    assert len(submitted) == len(persisted) == 16
    assert all(len(result["summary"]) == 12000 for result in persisted)
    assert all(result["gaps"] == ["An evidence gap"] for result in persisted)
    rows = state.get("phase_results", {})["reporting"]["analysis_ledger"]
    assert all("request_json" not in row for row in rows)
    assert all(len(row["result_content_sha256"]) == 64 for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "503", "invalid_response"])
async def test_native_ambiguous_submission_requires_explicit_new_attempt(monkeypatch, failure):
    state, config = fixture(native=True)
    state.get("phase_results", {})["reporting"]["analysis_ledger"][0]["started_at"] = utcnow_iso()
    calls = []
    fail = True

    class Adapter:
        async def submit_native_analysis(self, native_id, *, idempotency_key):
            calls.append(idempotency_key)
            if fail:
                if failure == "timeout":
                    raise httpx.ReadTimeout("acknowledgment lost")
                if failure == "503":
                    raise httpx.HTTPStatusError(
                        "unknown outcome",
                        request=httpx.Request("POST", "https://native.test"),
                        response=httpx.Response(503),
                    )
                return {"analysis_id": "remote-job", "state": "unexpected"}
            return {"analysis_id": "remote-job", "state": "queued"}

    class Resolver:
        async def resolve_with_connection_id(self, *_args, **_kwargs):
            return Adapter(), "connection-0"

        async def close(self):
            pass

    monkeypatch.setattr(module, "_make_analysis_resolver", Resolver)
    apply(state, await module.analysis_step(state, config))
    row = state.get("phase_results", {})["reporting"]["analysis_ledger"][0]
    assert row["status"] == "failed" and row["submission_uncertain"]
    assert module.route_analysis(state) == "analysis_recovery"
    assert len(calls) == 1
    assert "may create another job" in row["error"]
    original_key = row["idempotency_key"]
    monkeypatch.setattr(module, "interrupt", lambda _payload: {"action": "retry"})
    apply(state, module.analysis_recovery(state, config).update)
    retry = state.get("phase_results", {})["reporting"]["analysis_ledger"][0]
    assert retry["attempt"] == 2 and retry["idempotency_key"] != original_key
    assert not retry["submission_uncertain"]
    fail = False
    apply(state, await module.analysis_step(state, config))  # checkpoint deadline
    apply(state, await module.analysis_step(state, config))  # deliberate retry
    assert len(calls) == 2 and calls[0] != calls[1]
    assert (
        state.get("phase_results", {})["reporting"]["analysis_ledger"][0]["job_id"] == "remote-job"
    )


@pytest.mark.asyncio
async def test_frozen_handoff_digest_blocks_changed_submission(monkeypatch):
    state, config = fixture()
    state.get("phase_results", {})["execution"]["test_summary"]["kpis"]["p95_ms"] = 999

    class Resolver:
        async def resolve_with_connection_id(self, *_args, **_kwargs):
            pytest.fail("Changed evidence must not reach any connector")

        async def close(self):
            pass

    monkeypatch.setattr(module, "_make_analysis_resolver", Resolver)
    apply(state, await module.analysis_step(state, config))
    row = state.get("phase_results", {})["reporting"]["analysis_ledger"][0]
    assert row["status"] == "failed"
    assert "evidence changed" in row["error"]


@pytest.mark.asyncio
async def test_near_limit_provider_artifact_roundtrips_unchanged_through_report_reader(monkeypatch):
    from apex.graphs.pipeline import phase_subgraph
    from apex.graphs.pipeline.state import merge_artifacts

    state, config = fixture()
    row = state.get("phase_results", {})["reporting"]["analysis_ledger"][0]
    row.update(started_at=utcnow_iso(), job_id="large-result")
    result = AnalysisResult(
        schema_version=1,
        summary="Complete assessment",
        tool="other",
        tool_version="1",
        input_digest=row["input_digest"],
        findings=[{"claim": "x" * 1960} for _ in range(128)],
        warnings=["Uncertainty remains"],
        gaps=["A missing measurement"],
    )
    expected = result.model_dump_json().encode()
    assert 250_000 < len(expected) <= 256_000
    stored = {}

    class Adapter:
        async def status(self, job_id):
            return AnalysisJob(job_id=job_id, status="succeeded")

        async def result(self, _job_id):
            return result

    class Store:
        async def get(self, key):
            return stored[key]

    class Resolver:
        async def resolve_with_connection_id(self, kind, **kwargs):
            if kind is PortKind.ANALYSIS:
                return Adapter(), kwargs["connection_id"]
            return Store(), "store-1"

        async def close(self):
            pass

    async def persist(_store, **kwargs):
        stored[kwargs["artifact_key"]] = kwargs["payload"]
        return StoredArtifact(
            key=kwargs["artifact_key"], uri="memory://result", size=len(kwargs["payload"])
        )

    monkeypatch.setattr(module, "_make_analysis_resolver", Resolver)
    monkeypatch.setattr(module, "persist_artifact_with_intent", persist)
    monkeypatch.setattr(phase_subgraph, "_make_artifact_resolver", Resolver)
    update = await module.analysis_step(state, config)
    apply(state, update)
    state["artifacts"] = merge_artifacts(state.get("artifacts"), update["artifacts"])
    evidence = await phase_subgraph._load_analysis_report_evidence(
        state, state.get("phase_results", {})["reporting"], config
    )
    assert list(stored.values()) == [expected]
    assert evidence[0]["result"] == result.model_dump(mode="json")
    assert "attribution" not in result.findings[0]


@pytest.mark.parametrize("verified", [False, True])
def test_imported_execution_never_inherits_a_prior_managed_native_handle(verified):
    from apex.graphs.pipeline.configurable import PipelineConfigurable

    state, config = fixture(native=True)
    execution = state.get("phase_results", {})["execution"]
    execution.update(
        external=True,
        native_run_id="imported-native",
        native_connection_id="connection-0",
        native_source_verified=verified,
    )
    state["engine_handle"] = {"external_run_id": "old-managed-run", "connection_id": "connection-0"}
    evidence = module._execution_evidence(state, PipelineConfigurable.from_config(config))
    assert evidence["native_run_id"] == ("imported-native" if verified else None)
    assert evidence["native_connection_id"] == ("connection-0" if verified else None)
