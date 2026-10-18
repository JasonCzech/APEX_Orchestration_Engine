"""Final trusted binding checks before the engine can spend provider resources."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.cycle_executions import PreflightCheck
from apex.graphs.pipeline.configurable import Phase, PipelineConfigurable
from apex.persistence.models import CycleExecutionDispatch
from apex.services import cycle_dispatch as service


@pytest.fixture
def binding(monkeypatch):
    actor = ConsumerIdentity(
        consumer_id="operator",
        name="Operator",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="project", app_id="app")],
    )
    row = CycleExecutionDispatch(
        id="dispatch",
        cycle_id="cycle",
        preparation_id="prep",
        thread_id="thread",
        active_environment_id="environment",
        created_by="operator",
    )
    session = AsyncMock()
    session.scalar.return_value = row

    @asynccontextmanager
    async def context_manager():
        yield session

    monkeypatch.setattr(service, "get_sessionmaker", lambda: context_manager)
    monkeypatch.setattr(service, "_current_actor", AsyncMock(return_value=actor))
    cycle = SimpleNamespace(project_id="project", app_id="app")
    snapshot = SimpleNamespace(
        environment_id="environment",
        environment_version=4,
        connection_id="engine",
        plan_title="Reviewed plan",
        workload={"vusers": 11, "ramp_s": 2, "duration_s": 60, "slas": {"p95_ms": 500}},
        script_source_checksum="exact-checksum",
    )
    artifact = SimpleNamespace(connection_id="store", artifact_key="exact-key")
    target = SimpleNamespace(base_url="https://example.com")
    checks = []
    monkeypatch.setattr(
        service,
        "dispatch_context",
        AsyncMock(return_value=(cycle, snapshot, artifact, target, checks)),
    )
    cfg = PipelineConfigurable(
        cycle_dispatch_id="dispatch",
        project_id="project",
        app_id="app",
        environment_id="environment",
        environment_target="https://example.com",
        environment_target_version=4,
        engine="apex_load",
        connections={"execution_engine": "engine", "artifact_store": "store"},
        phases=[Phase.EXECUTION],
    )
    return cfg, row, checks, session


@pytest.mark.asyncio
async def test_exact_approved_workload_does_not_include_inline_or_default_script(binding):
    cfg, _, _, _ = binding
    spec, source = await service.load_dispatch_binding(cfg, "thread", 1)
    assert spec.script_refs == [] and source is None
    assert spec.vusers == 11 and spec.duration_s == 60 and spec.slas == {"p95_ms": 500}
    assert spec.target_environment == "https://example.com"
    assert spec.idempotency_key == "thread-execution-a1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("project_id", "other"),
        ("app_id", "other"),
        ("environment_target", "https://other.example"),
        ("environment_target_version", 5),
        ("engine", "sim"),
        ("load_test", {"duration_s": 900}),
        ("connections", {"execution_engine": "engine", "artifact_store": "other"}),
        ("phases", ["script_scenario", "execution"]),
    ],
)
async def test_configuration_cannot_change_frozen_authority(binding, field, value):
    cfg, _, _, _ = binding
    cfg = cfg.model_copy(update={field: value})
    with pytest.raises(ValueError, match="exact cycle binding"):
        await service.load_dispatch_binding(cfg, "thread", 1)


@pytest.mark.asyncio
async def test_revoked_approval_and_thread_mismatch_fail_before_source_io(binding):
    cfg, row, checks, _ = binding
    checks.append(
        PreflightCheck(key="approval", label="Approval", status="blocked", detail="Changed")
    )
    with pytest.raises(ValueError, match="prerequisites changed"):
        await service.load_dispatch_binding(cfg, "thread", 1, read_source=True)
    row.thread_id = "different"
    with pytest.raises(ValueError, match="does not own"):
        await service.load_dispatch_binding(cfg, "thread", 1, read_source=True)


@pytest.mark.asyncio
async def test_source_is_read_by_exact_recorded_artifact_and_checksum(binding, monkeypatch):
    from apex.routers import cycle_scripts

    cfg, _, _, _ = binding
    store = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(cycle_scripts, "resolve_store", AsyncMock(return_value=store))
    read = AsyncMock(return_value=b'{"name":"exact","config":{"base_url":"https://example.com"}}')
    monkeypatch.setattr(service, "read_verified_script", read)
    _, source = await service.load_dispatch_binding(cfg, "thread", 1, read_source=True)
    assert source == '{"name":"exact","config":{"base_url":"https://example.com"}}'
    read.assert_awaited_once_with(store, "exact-key", "exact-checksum")
    store.aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled,deleted,role,scope,allowed",
    [
        (True, None, "operator", "app", True),
        (False, None, "operator", "app", False),
        (True, datetime.now(UTC), "operator", "app", False),
        (True, None, "viewer", "app", False),
    ],
)
async def test_current_persisted_actor_must_still_be_an_enabled_operator(
    enabled, deleted, role, scope, allowed
):
    session = AsyncMock()
    session.scalar.return_value = SimpleNamespace(
        id="operator",
        name="Operator",
        consumer_type="dashboard",
        enabled=enabled,
        deleted_at=deleted,
        role=role,
    )
    session.scalars.return_value = Mock(
        all=Mock(return_value=[SimpleNamespace(project_id="project", app_id=scope)])
    )
    if allowed:
        actor = await service._current_actor(session, "operator")
        assert actor.allows_scope(project_id="project", app_id="app")
    else:
        with pytest.raises(ValueError):
            await service._current_actor(session, "operator")


@pytest.fixture
def preview(monkeypatch):
    import hashlib
    import json

    from apex.routers import cycle_executions, cycle_script_validation
    from tests.unit.test_apex_load_approved_source import SOURCE
    from tests.unit.test_cycle_script_validation import row, snapshot

    source = json.dumps(SOURCE)
    prepared = snapshot().model_copy(
        update={
            "script_source_checksum": hashlib.sha256(source.encode()).hexdigest(),
            "workload": {"vusers": 1, "ramp_s": 0, "duration_s": 5, "slas": {}},
        }
    )
    cycle = SimpleNamespace(
        id="cycle",
        project_id="project",
        app_id="app",
        versions=[{"id": "script", "content": {"source": source}}],
    )
    artifact = SimpleNamespace(connection_id="store")
    target = SimpleNamespace(base_url="https://target.example", version=1)
    engine = SimpleNamespace(
        options={"native_control_enabled": True, "project_id": "provider-project"}
    )
    validation = row()
    session = AsyncMock()
    session.scalar.side_effect = lambda statement: (
        validation if "cycle_script_validations" in str(statement) else engine
    )
    actor = ConsumerIdentity(
        consumer_id="operator",
        name="Operator",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="project", app_id="app")],
    )
    monkeypatch.setattr(service, "_current_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(cycles_enabled=True))
    monkeypatch.setattr(service, "execution_checks", lambda *a, **k: [])
    monkeypatch.setattr(
        cycle_script_validation,
        "validation_context",
        AsyncMock(return_value=(cycle, prepared, artifact, [])),
    )
    monkeypatch.setattr(cycle_executions, "context", AsyncMock(return_value=(target, [], None)))
    return session, actor, prepared, cycle, engine, validation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "bootstrap",
        "revoked_scope",
        "native_schedule",
        "native_control",
        "release_mapping",
        "stale_validation",
        "unsupported_source",
    ],
)
async def test_dispatch_preview_blocks_current_authority_and_unsupported_exact_source_before_intent(
    preview, monkeypatch, fault
):
    import hashlib
    import json
    from datetime import timedelta

    session, actor, prepared, cycle, engine, validation = preview
    if fault == "bootstrap":
        monkeypatch.setattr(
            service, "_current_actor", AsyncMock(side_effect=ValueError("not persisted"))
        )
    elif fault == "revoked_scope":
        monkeypatch.setattr(
            service,
            "_current_actor",
            AsyncMock(return_value=actor.model_copy(update={"scopes": []})),
        )
    elif fault == "native_schedule":
        prepared.launch_authority = "native_schedule"
    elif fault == "native_control":
        engine.options["native_control_enabled"] = False
    elif fault == "release_mapping":
        engine.options["release_mapping"] = {"enabled": True}
    elif fault == "stale_validation":
        validation.finished_at = datetime.now(UTC) - timedelta(days=2)
    elif fault == "unsupported_source":
        parsed = json.loads(cycle.versions[0]["content"]["source"])
        parsed["protocol"] = "websocket"
        source = json.dumps(parsed)
        cycle.versions[0]["content"]["source"] = source
        prepared.script_source_checksum = hashlib.sha256(source.encode()).hexdigest()
    *_, checks = await service.dispatch_context(session, actor, "cycle", "prep")
    assert any(c.status == "blocked" for c in checks) is (fault is not None)
    assert any(c.key == "provider_access" and c.status == "not_checked" for c in checks)
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper",
    [None, "attempt", "artifact", "workload", "external", "scope", "connection", "unprovisioned"],
)
async def test_result_binding_requires_exact_persisted_provider_receipts(preview, tamper):
    from apex.adapters.apex_load.engine import approved_workload_digest
    from apex.persistence.models import CycleExecutionPreparation, EngineRun

    session, _, prepared, cycle, _, _ = preview
    cycle.environment_id = "environment"
    cfg = {
        "cycle_dispatch_id": "dispatch",
        "project_id": "project",
        "app_id": "app",
        "environment_id": "environment",
        "environment_target": "https://target.example",
        "environment_target_version": 1,
        "engine": "apex_load",
        "connections": {"execution_engine": "engine"},
    }
    dispatch = CycleExecutionDispatch(id="dispatch", thread_id="thread", pipeline_config=cfg)
    prep = CycleExecutionPreparation(snapshot=prepared.model_dump())
    session.execute.return_value = Mock(all=Mock(return_value=[(dispatch, prep)]))
    run = EngineRun(
        id="run",
        thread_id="thread",
        attempt=1,
        engine="apex_load",
        project_id="project",
        app_id="app",
        ownership_known=True,
        scope_ownership_known=True,
        external_run_id="provider-run",
        handle={
            "connection_id": "engine",
            "external_run_id": "provider-run",
            "idempotency_key": "thread-execution-a1",
            "extras": {
                "approved_script_id": "script",
                "approved_artifact_sha256": prepared.script_source_checksum,
                "approved_workload_digest": approved_workload_digest(
                    service.binding_spec(prepared, "https://target.example", "thread-execution-a1")
                ),
            },
        },
    )
    if tamper == "attempt":
        run.attempt = 2
    elif tamper == "artifact":
        run.handle["extras"]["approved_artifact_sha256"] = "different"
    elif tamper == "workload":
        run.handle["extras"]["approved_workload_digest"] = "different"
    elif tamper == "external":
        run.external_run_id = "other"
    elif tamper == "scope":
        run.app_id = "other"
    elif tamper == "connection":
        run.handle["connection_id"] = "other"
    elif tamper == "unprovisioned":
        run.handle = {}
    assert await service.verified_dispatch_runs(session, cycle, [run]) == (
        {"run"} if tamper is None else set()
    )
