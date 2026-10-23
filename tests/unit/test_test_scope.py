"""Combined-system contract, target independence, snapshots and revocation."""

import copy
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.pipeline import Phase
from apex.domain.test_scope import TestScopeV1
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.graphs.pipeline.execution_phase import _verified_stamped_target
from apex.graphs.pipeline.phase_subgraph import _compose_user
from apex.persistence.models import Base, RunParticipant, RunScopeBinding
from apex.services.test_scope import (
    allows_test_scope,
    can_access_thread,
    participant_visibility,
    persist_test_scope,
    resolve_test_scope,
    verify_persisted_test_scope,
)


def topology():
    return {
        "schema_version": 1,
        "participants": [
            {
                "id": name,
                "project_id": f"project-{i}",
                "application_id": f"app-{i}",
                "environment_id": f"env-{i}",
                "layer": layer,
            }
            for i, (name, layer) in enumerate(
                [
                    ("web", "frontend"),
                    ("api", "middle"),
                    ("queue", "middle"),
                    ("database", "backend"),
                ]
            )
        ],
        "primary_participant_id": "api",
        "traffic_entry_participant_id": "web",
    }


def identity(count=4):
    return ConsumerIdentity(
        consumer_id="tester",
        name="Tester",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id=f"project-{i}", app_id=f"app-{i}") for i in range(count)],
    )


@pytest.mark.parametrize("primary", ["web", "api", "queue", "database"])
def test_primary_can_be_any_layer_and_does_not_redirect_traffic(primary):
    raw = topology()
    raw["primary_participant_id"] = primary
    cfg = PipelineConfigurable(
        test_scope=TestScopeV1.model_validate(raw),
        engine="apex_load",
        environment_target="https://web.example",
        environment_target_version=2,
    )
    assert cfg.app_id == next(
        p["application_id"] for p in raw["participants"] if p["id"] == primary
    )
    assert cfg.traffic_app_id == "app-0"
    assert cfg.environment_id == "env-0"
    assert (
        _verified_stamped_target(cfg, "https://web.example", 2, current_app_id="app-0")
        == "https://web.example"
    )
    with pytest.raises(ValueError, match="changed"):
        _verified_stamped_target(cfg, "https://web.example", 3, current_app_id="app-0")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda v: v.update(primary_participant_id="missing"),
        lambda v: v.update(traffic_entry_participant_id="missing"),
        lambda v: v["participants"].pop(),
        lambda v: v["participants"][1].update(layer="frontend"),
        lambda v: v["participants"].append(copy.deepcopy(v["participants"][0])),
        lambda v: v["participants"][1].update(application_id="app-0", environment_id="env-0"),
    ],
)
def test_invalid_topologies_rejected(mutation):
    raw = topology()
    mutation(raw)
    with pytest.raises(ValidationError):
        TestScopeV1.model_validate(raw)


async def test_resolution_and_downstream_context(monkeypatch):
    scope = TestScopeV1.model_validate(topology())
    repository = SimpleNamespace(get_environment=AsyncMock())

    def environment(environment_id):
        i = int(environment_id[-1])
        return SimpleNamespace(
            id=environment_id,
            application_id=f"app-{i}",
            name=f"Environment {i}",
            kind="service",
            options={"password": "never include options"},
            application=SimpleNamespace(
                project_ids=[f"project-{i}"],
                archived_at=None,
                name=f"System {i}",
                description=f"Backend context {i}",
            ),
        )

    repository.get_environment.side_effect = environment
    target = AsyncMock(return_value=SimpleNamespace(base_url="https://web.example", version=7))
    monkeypatch.setattr("apex.services.test_scope.resolve_environment_target", target)
    snapshot = await resolve_test_scope(repository, identity(), scope, live=True)
    assert repository.get_environment.await_count == 4
    target.assert_awaited_once_with(repository, "env-0", project_id="project-0", app_id="app-0")
    assert "password" not in str(snapshot)
    cfg = PipelineConfigurable(test_scope=scope, test_scope_snapshot=snapshot)
    saved = cfg.snapshot()
    restored = PipelineConfigurable.from_state_for_phase(
        {"run_config": saved, "phases_plan": [p.value for p in cfg.selected_phases()]},
        {"configurable": saved},
        Phase.REPORTING,
    )
    assert restored.test_scope == scope
    prompt = _compose_user({"run_config": saved}, Phase.REPORTING, {"user": "Report"}, {})
    assert "Backend context 3" in prompt and '"primary_participant_id": "api"' in prompt
    assert "run-level/unattributed" in prompt
    with pytest.raises(HTTPException) as denied:
        await resolve_test_scope(repository, identity(3), scope, live=False)
    assert denied.value.status_code == 403
    no_entry = scope.model_copy(update={"traffic_entry_participant_id": None})
    await resolve_test_scope(repository, identity(), no_entry, live=False)
    with pytest.raises(HTTPException):
        await resolve_test_scope(repository, identity(), no_entry, live=True)


@pytest.fixture
async def database(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://", execution_options={"schema_translate_map": {"apex": None}}
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda conn: Base.metadata.create_all(
                conn,
                tables=[
                    cast(Table, RunParticipant.__table__),
                    cast(Table, RunScopeBinding.__table__),
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("apex.persistence.db.get_sessionmaker", lambda: factory)
    yield factory
    await engine.dispose()


async def test_projection_is_immutable_and_access_revokes(database):
    scope = TestScopeV1.model_validate(topology())
    await persist_test_scope("run-one", scope)
    await persist_test_scope("run-one", scope)
    assert await can_access_thread(identity(), "run-one")
    assert not await can_access_thread(identity(3), "run-one")
    assert not allows_test_scope(identity(3), scope)
    await verify_persisted_test_scope(identity(), "run-one", scope)
    with pytest.raises(HTTPException):
        await verify_persisted_test_scope(identity(), "unknown", scope)
    with pytest.raises(ValueError):
        await persist_test_scope(
            "run-one", scope.model_copy(update={"primary_participant_id": "web"})
        )
    async with database() as session:
        assert len((await session.scalars(select(RunParticipant))).all()) == 4
        from sqlalchemy.orm import aliased

        run = aliased(RunParticipant)
        query = (
            select(run.thread_id)
            .distinct()
            .where(participant_visibility(run.thread_id, identity(3).scopes))
        )
        assert (await session.scalars(query)).all() == []
        query = (
            select(run.thread_id)
            .distinct()
            .where(participant_visibility(run.thread_id, identity().scopes))
        )
        assert (await session.scalars(query)).all() == ["run-one"]
