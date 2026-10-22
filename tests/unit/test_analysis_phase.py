"""Analysis jobs are not complete until validated evidence is durably collected."""

import json
from typing import cast

import httpx
import pytest
from langchain_core.runnables import RunnableConfig

from apex.graphs.pipeline import analysis_phase as module
from apex.graphs.pipeline.state import PipelineState
from apex.ports.analysis import AnalysisCapabilities, AnalysisJob, AnalysisResult
from apex.ports.artifact_store import StoredArtifact


def fixture():
    config = cast(
        RunnableConfig,
        {
            "configurable": {
                "thread_id": "analysis-test",
                "engine": "loadrunner",
                "phases": ["reporting"],
                "analysis": {
                    "selections": [{"tool": "perfreportforge", "connection_id": "analysis-1"}]
                },
            }
        },
    )
    state = cast(
        PipelineState,
        {
            "phase_results": {
                "reporting": {"attempt": 1},
                "execution": {"test_summary": {"engine": "loadrunner", "kpis": {"p95_ms": 50}}},
            }
        },
    )
    state.get("phase_results", {})["reporting"].update(
        module.prepare_analysis(state, config)["phase_results"]["reporting"]
    )
    return state, config


def apply(state, update):
    state.get("phase_results", {})["reporting"].update(update["phase_results"]["reporting"])


@pytest.mark.asyncio
async def test_collection_failure_never_finishes_or_resubmits_job(monkeypatch):
    state, config = fixture()
    submitted = []
    closed = []
    stored = []

    class Adapter:
        fail_result = True

        async def capabilities(self):
            return AnalysisCapabilities(
                schema_version=1,
                engines=["loadrunner"],
                evidence_kinds=["test_summary"],
                available=True,
            )

        async def submit(self, request):
            submitted.append(request)
            return AnalysisJob(job_id="remote-1", status="succeeded")

        async def status(self, job_id):
            return AnalysisJob(job_id=job_id, status="succeeded")

        async def result(self, job_id):
            if self.fail_result:
                raise httpx.HTTPStatusError(
                    "temporary",
                    request=httpx.Request("GET", "https://analysis.test"),
                    response=httpx.Response(503),
                )
            return AnalysisResult(
                schema_version=1,
                summary="Found slow checkout",
                tool="perfreportforge",
                tool_version="1",
                input_digest=submitted[0]["input_digest"],
                findings=[{"claim": "Slow checkout", "evidence": ["run/1"]}],
            )

    adapter = Adapter()

    class Resolver:
        async def resolve_with_connection_id(self, kind, **kwargs):
            return (adapter, "analysis-1") if kind.value == "analysis" else (object(), "store-1")

        async def close(self):
            closed.append(True)

    async def persist(store, **kwargs):
        stored.append(kwargs["payload"])
        return StoredArtifact(
            key=kwargs["artifact_key"], uri="memory://analysis/result", size=len(kwargs["payload"])
        )

    async def no_sleep(seconds):
        pass

    monkeypatch.setattr(module, "_make_analysis_resolver", Resolver)
    monkeypatch.setattr(module, "persist_artifact_with_intent", persist)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    # Write-ahead deadline, submission and returned remote identity each checkpoint separately.
    apply(state, await module.analysis_step(state, config))
    apply(state, await module.analysis_step(state, config))
    apply(state, await module.analysis_step(state, config))
    row = state.get("phase_results", {})["reporting"]["analysis_ledger"][0]
    assert row["status"] == "collecting"
    assert module.route_analysis(state) == "analysis_step"
    assert not stored
    adapter.fail_result = False
    update = await module.analysis_step(state, config)
    apply(state, update)
    assert module.route_analysis(state) == "report_agent"
    assert len(submitted) == 1
    assert len(closed) == 4
    assert json.loads(stored[0])["findings"][0]["claim"] == "Slow checkout"
    assert update["artifacts"][0]["kind"] == "analysis"


def test_retry_preserves_known_job_except_terminal_provider_failure(monkeypatch):
    state, config = fixture()
    row = state.get("phase_results", {})["reporting"]["analysis_ledger"][0]
    row.update(status="failed", job_id="known", error="deadline")
    key = row["idempotency_key"]
    monkeypatch.setattr(module, "interrupt", lambda payload: {"action": "retry"})
    retry = module.analysis_recovery(state, config)
    apply(state, retry.update)
    row = state.get("phase_results", {})["reporting"]["analysis_ledger"][0]
    assert row["job_id"] == "known"
    assert row["idempotency_key"] == key
    row.update(status="failed", provider_terminal_failure=True)
    retry = module.analysis_recovery(state, config)
    assert isinstance(retry.update, dict)
    next_row = retry.update["phase_results"]["reporting"]["analysis_ledger"][0]
    assert next_row["job_id"] is None
    assert next_row["idempotency_key"] != key


def test_analysis_result_rejects_credentials_and_wrong_digest():
    with pytest.raises(ValueError, match="credential"):
        AnalysisResult(
            schema_version=1,
            summary="Assessment",
            tool="internal_rca",
            tool_version="2",
            input_digest="a" * 64,
            findings=[{"Authorization": "Bearer review-canary-credential"}],
        )
    with pytest.raises(ValueError):
        AnalysisResult(
            schema_version=1,
            summary="Assessment",
            tool="internal_rca",
            tool_version="2",
            input_digest="wrong",
        )


@pytest.mark.asyncio
async def test_http_analysis_contract_uses_bearer_and_stable_key(monkeypatch):
    from apex.adapters import analysis as adapter_module
    from apex.adapters.analysis import HttpAnalysisAdapter
    from apex.adapters.registry import ConnectionConfig, PortKind
    from apex.domain.integrations import SecretValue

    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/v1/capabilities":
            return httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "engines": ["loadrunner"],
                    "evidence_kinds": ["test_summary"],
                    "available": True,
                },
            )
        return httpx.Response(202, json={"job_id": "remote", "status": "queued"})

    monkeypatch.setattr(
        adapter_module,
        "safe_async_http_client",
        lambda **kwargs: httpx.AsyncClient(
            base_url=kwargs["base_url"],
            headers=kwargs["headers"],
            transport=httpx.MockTransport(handler),
        ),
    )
    adapter = HttpAnalysisAdapter(
        ConnectionConfig(
            id="a",
            name="Analysis",
            kind=PortKind.ANALYSIS,
            provider="perfreportforge",
            options={"base_url": "https://analysis.example.com"},
        ),
        SecretValue(value="test-token"),
    )
    assert (await adapter.capabilities()).available
    assert (await adapter.submit({"idempotency_key": "stable"})).job_id == "remote"
    assert calls[-1].headers["Idempotency-Key"] == "stable"
    assert calls[-1].headers["Authorization"] == "Bearer test-token"


def test_native_job_identity_rejects_credentials_before_checkpoint():
    with pytest.raises(ValueError, match="credential") as exc:
        AnalysisJob(job_id="Bearer review-job-canary", status="queued")
    assert "review-job-canary" not in str(exc.value)


def test_recovery_rejects_credential_bearing_decisions(monkeypatch):
    state, config = fixture()
    state.get("phase_results", {})["reporting"]["analysis_ledger"][0].update(
        status="failed", error="Failed"
    )
    payloads = []

    def resume(payload):
        payloads.append(dict(payload))
        return (
            {"action": "assign_hitl", "note": "Bearer recovery-canary"}
            if len(payloads) == 1
            else {"action": "retry"}
        )

    monkeypatch.setattr(module, "interrupt", resume)
    assert module.analysis_recovery(state, config).goto == "analysis_step"
    assert "credential" in payloads[1]["error"]


def test_ordered_multiple_tools_keep_successful_evidence_on_retry(monkeypatch):
    state, config = fixture()
    config.setdefault("configurable", {})["analysis"] = {
        "selections": [
            {"tool": "other", "connection_id": "other-2"},
            {"tool": "perfreportforge", "connection_id": "forge"},
            {"tool": "other", "connection_id": "other-1"},
            {"tool": "lra_engine", "connection_id": "lra"},
        ]
    }
    del state.get("phase_results", {})["reporting"]["analysis_ledger"]
    apply(state, module.prepare_analysis(state, config))
    ledger = state.get("phase_results", {})["reporting"]["analysis_ledger"]
    assert [row["connection_id"] for row in ledger] == ["lra", "forge", "other-2", "other-1"]
    ledger[0].update(status="succeeded", result_artifact_id="successful-lra")
    ledger[1].update(status="failed", provider_terminal_failure=True, error="Failed")
    monkeypatch.setattr(module, "interrupt", lambda payload: {"action": "retry"})
    retry = module.analysis_recovery(state, config)
    apply(state, retry.update)
    assert state.get("phase_results", {})["reporting"]["analysis_ledger"][0] == ledger[0]
    assert state.get("phase_results", {})["reporting"]["analysis_ledger"][1]["attempt"] == 2


@pytest.mark.asyncio
async def test_deadline_fails_without_invoking_connector(monkeypatch):
    state, config = fixture()
    row = state.get("phase_results", {})["reporting"]["analysis_ledger"][0]
    row["started_at"] = "2000-01-01T00:00:00+00:00"
    closed = []

    class Resolver:
        async def resolve_with_connection_id(self, *args, **kwargs):
            pytest.fail("Expired analysis invoked connector")

        async def close(self):
            closed.append(True)

    monkeypatch.setattr(module, "_make_analysis_resolver", Resolver)
    apply(state, await module.analysis_step(state, config))
    failed = state.get("phase_results", {})["reporting"]["analysis_ledger"][0]
    assert failed["status"] == "failed"
    assert "deadline" in failed["error"]
    assert closed == [True]


def test_analysis_polling_budget_does_not_depend_on_execution_timeout():
    from apex.domain.phase_execution import AnalysisConfiguration
    from apex.graphs.pipeline.configurable import Limits
    from apex.graphs.pipeline.execution_phase import recommended_recursion_limit

    analysis = AnalysisConfiguration.model_validate(
        {
            "timeout_s": 1800,
            "selections": [
                {"tool": "other", "connection_id": "one"},
                {"tool": "other", "connection_id": "two"},
            ],
        }
    )
    limits = Limits(poll_timeout_s=1, poll_interval_s=1)
    assert (
        recommended_recursion_limit(limits, analysis) >= recommended_recursion_limit(limits) + 140
    )
