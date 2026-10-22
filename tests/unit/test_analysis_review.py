"""Independent regressions at analysis persistence and adapter lease boundaries."""

import asyncio
import json
from typing import cast

import pytest
from langchain_core.runnables import RunnableConfig

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.pipeline import utcnow_iso
from apex.graphs.pipeline import analysis_phase as module
from apex.graphs.pipeline.state import PipelineState
from apex.ports.analysis import AnalysisCapabilities, AnalysisJob, AnalysisResult
from apex.ports.artifact_store import StoredArtifact
from apex.services.connections import ConnectionResolver


@pytest.fixture(autouse=True)
def isolated_native_receipts(monkeypatch):
    """Lifecycle unit fixtures isolate persistence; receipt tests exercise SQL."""

    async def claim(_scope):
        return None

    async def record(_scope, _job_id):
        return None

    monkeypatch.setattr(module, "claim_native_analysis", claim)
    monkeypatch.setattr(module, "record_native_analysis_receipt", record)


def _fixture(*, native: bool = False):
    config = cast(
        RunnableConfig,
        {
            "configurable": {
                "thread_id": "analysis-review",
                "engine": "apex_load" if native else "loadrunner",
                "phases": ["reporting"],
                "connections": {"artifact_store": "store-1"},
                "analysis": {
                    "selections": [
                        {
                            "tool": "internal_rca" if native else "perfreportforge",
                            "connection_id": "analysis-1",
                        }
                    ]
                },
            }
        },
    )
    state = cast(
        PipelineState,
        {
            "phase_results": {
                "reporting": {"attempt": 1},
                "execution": {
                    "test_summary": {"kpis": {"p95_ms": 50}},
                    "engine_handle": {
                        "external_run_id": "native-run-1",
                        "connection_id": "analysis-1",
                    },
                },
            }
        },
    )
    entry = module.prepare_analysis(state, config)["phase_results"]["reporting"]
    state.get("phase_results", {})["reporting"].update(entry)
    row = entry["analysis_ledger"][0]
    row["started_at"] = utcnow_iso()
    return state, config, row


@pytest.mark.parametrize("boundary", ["job", "result"])
async def test_native_credential_rejection_precedes_checkpoint_and_artifact_write(
    monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    state, config, row = _fixture(native=True)
    if boundary == "result":
        row["job_id"] = "native-job"
    closed = []

    class Adapter:
        async def submit_native_analysis(self, *_args, **_kwargs):
            return {"analysis_id": "Bearer review-native-canary", "state": "queued"}

        async def get_native_analysis(self, *_args):
            return {
                "analysis_id": "native-job",
                "state": "completed",
                "assessment": {"Authorization": "Bearer review-native-canary"},
            }

        async def aclose(self):
            closed.append("adapter")

    class Resolver:
        async def resolve_with_connection_id(self, kind, **_kwargs):
            assert kind is PortKind.EXECUTION_ENGINE
            return Adapter(), "analysis-1"

        async def close(self):
            closed.append("resolver")

    monkeypatch.setattr(module, "_make_analysis_resolver", Resolver)
    update = await module.analysis_step(state, config)
    saved = update["phase_results"]["reporting"]["analysis_ledger"][0]
    assert saved["status"] == "failed"
    assert saved["job_id"] == ("native-job" if boundary == "result" else None)
    assert saved["error"]
    assert "review-native-canary" not in json.dumps(update)
    assert "artifacts" not in update
    assert closed == ["adapter", "resolver"]


@pytest.mark.parametrize("outcome", ["success", "wrong_tool", "unsupported_evidence", "cancel"])
async def test_real_resolver_releases_analysis_and_artifact_leases_before_return(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    state, config, row = _fixture()
    if outcome != "unsupported_evidence":
        row["job_id"] = "remote-job"
    closed = []
    persisted = []

    class Adapter:
        async def capabilities(self):
            return AnalysisCapabilities(
                schema_version=1, engines=["loadrunner"], evidence_kinds=[], available=True
            )

        async def submit(self, _request):
            pytest.fail("Incompatible evidence must not submit a remote job")

        async def status(self, job_id):
            return AnalysisJob(job_id=job_id, status="succeeded")

        async def result(self, _job_id):
            if outcome == "cancel":
                raise asyncio.CancelledError()
            return AnalysisResult(
                schema_version=1,
                summary="Validated findings",
                tool="lra_engine" if outcome == "wrong_tool" else "perfreportforge",
                tool_version="1",
                input_digest=row["input_digest"],
                findings=[{"claim": "A slow endpoint"}],
            )

        async def aclose(self):
            closed.append("adapter")

    class Store:
        async def aclose(self):
            closed.append("store")

    async def build(conn, _secrets):
        return Adapter() if conn.kind is PortKind.ANALYSIS else Store()

    async def persist(_store, **kwargs):
        persisted.append(kwargs["payload"])
        return StoredArtifact(
            key=kwargs["artifact_key"], uri="memory://review", size=len(kwargs["payload"])
        )

    resolver = ConnectionResolver(
        [
            ConnectionConfig(
                id="analysis-1",
                kind=PortKind.ANALYSIS,
                provider="perfreportforge",
                name="Analysis",
            ),
            ConnectionConfig(
                id="store-1", kind=PortKind.ARTIFACT_STORE, provider="stub", name="Artifacts"
            ),
        ]
    )
    monkeypatch.setattr(AdapterRegistry, "build", staticmethod(build))
    monkeypatch.setattr(module, "_make_analysis_resolver", lambda: resolver)
    monkeypatch.setattr(module, "persist_artifact_with_intent", persist)
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await module.analysis_step(state, config)
    else:
        update = await module.analysis_step(state, config)
        saved = update["phase_results"]["reporting"]["analysis_ledger"][0]
        assert saved["status"] == ("succeeded" if outcome == "success" else "failed")
        if outcome == "wrong_tool":
            assert "different tool" in saved["error"]
        elif outcome == "unsupported_evidence":
            assert "does not accept test summary evidence" in saved["error"]
        else:
            assert update["artifacts"][0]["artifact_connection_id"] == "store-1"
    assert len(persisted) == (1 if outcome == "success" else 0)
    assert sorted(closed) == (["adapter", "store"] if outcome == "success" else ["adapter"])
