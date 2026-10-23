"""Prompt usage: launches index the prompt versions they pinned ("Used by").

Recording is best-effort and idempotent; reads only show tests the caller can
see, and the launch facade records usage once its run exists.
"""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import apex.services.pipeline_read as pipeline_read
import apex.services.prompt_usage as prompt_usage
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.models import Base, PromptUsage, RunParticipant, RunScopeBinding
from apex.services.pipeline_read import PipelineReadService
from apex.services.prompt_usage import list_prompt_usage, record_prompt_usage
from tests.unit.test_pipelines_create_run import FakeClient


def _entry(namespace: str, prompt_id: str, version: int = 1, **metadata: Any) -> dict[str, Any]:
    return {
        "namespace": namespace,
        "key": prompt_id,
        "prompt_id": prompt_id,
        "version_id": f"{prompt_id}-v{version}",
        "version": version,
        "content": f"{prompt_id} text",
        **metadata,
    }


def _configurable(project_id: str | None = "proj-1", app_id: str | None = None) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "app_id": app_id,
        "prompt_manifest": {
            "system": _entry("system", "sys", 2),
            "phases": {
                "story_analysis": _entry("phase", "story", phase="story_analysis"),
                "reporting": _entry("phase", "report", phase="reporting"),
            },
            "application": [_entry("application", "shared-ctx", is_shared=True)],
            "environment": [],
        },
    }


def _identity(project_ids: list[str], role: Role = Role.VIEWER) -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="c1",
        name="c1",
        consumer_type=ConsumerType.DASHBOARD,
        role=role,
        scopes=[ScopeRef(project_id=pid) for pid in project_ids],
    )


@pytest.fixture
async def database(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://", execution_options={"schema_translate_map": {"apex": None}}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(prompt_usage, "get_sessionmaker", lambda: factory)
    yield factory
    await engine.dispose()


async def test_a_launch_records_each_pinned_slot_once(database) -> None:
    await record_prompt_usage("thr-1", _configurable(), title="Checkout soak")
    await record_prompt_usage("thr-1", _configurable(), title="Checkout soak")

    async with database() as session:
        rows = (await session.scalars(select(PromptUsage).order_by(PromptUsage.slot))).all()
    assert [(row.prompt_id, row.slot, row.version) for row in rows] == [
        ("shared-ctx", "application", 1),
        ("report", "phase:reporting", 1),
        ("story", "phase:story_analysis", 1),
        ("sys", "system", 2),
    ]
    assert {(row.thread_id, row.project_id, row.title) for row in rows} == {
        ("thr-1", "proj-1", "Checkout soak")
    }


async def test_recording_is_skipped_without_a_manifest_and_never_raises(database, monkeypatch):
    await record_prompt_usage("thr-1", {"project_id": "proj-1"})
    await record_prompt_usage(None, _configurable())

    def broken():
        raise ConnectionError("database down")

    monkeypatch.setattr(prompt_usage, "get_sessionmaker", broken)
    await record_prompt_usage("thr-2", _configurable())  # logged, not raised

    async with database() as session:
        assert (await session.scalars(select(PromptUsage))).all() == []


async def test_reads_list_each_visible_test_once_newest_first(database) -> None:
    await record_prompt_usage("thr-old", _configurable("proj-1"), title="Old run")
    await record_prompt_usage("thr-other", _configurable("proj-2"), title="Other project")
    await record_prompt_usage("thr-none", _configurable(None), title="No project")
    await record_prompt_usage("thr-new", _configurable("proj-1"), title="New run")
    async with database() as session:
        # sqlite's CURRENT_TIMESTAMP is per second; order the runs explicitly.
        for index, thread_id in enumerate(["thr-old", "thr-other", "thr-none", "thr-new"]):
            query = select(PromptUsage).where(PromptUsage.thread_id == thread_id)
            for row in (await session.scalars(query)).all():
                row.recorded_at = row.recorded_at.replace(second=index)
        await session.commit()

    total, tests = await list_prompt_usage("sys", _identity(["proj-1"]), limit=10)
    assert total == 2
    assert [(test.thread_id, test.title, test.version) for test in tests] == [
        ("thr-new", "New run", 2),
        ("thr-old", "Old run", 2),
    ]

    total, tests = await list_prompt_usage("sys", _identity([], role=Role.ADMIN), limit=2)
    assert total == 4
    assert [test.thread_id for test in tests] == ["thr-new", "thr-none"]


async def test_app_scopes_and_combined_runs_hide_tests_the_caller_cannot_open(database) -> None:
    await record_prompt_usage("thr-project", _configurable("proj-1", None), title="Project-wide")
    await record_prompt_usage("thr-app", _configurable("proj-1", "app-a"), title="App A")
    await record_prompt_usage("thr-combined", _configurable("proj-1", "app-a"), title="Combined")
    async with database() as session:
        # The combined run also covers proj-2/app-b.
        session.add(
            RunScopeBinding(
                thread_id="thr-combined", scope={}, participant_count=2, scope_digest="d"
            )
        )
        for pid, project, app in (("p1", "proj-1", "app-a"), ("p2", "proj-2", "app-b")):
            session.add(
                RunParticipant(
                    thread_id="thr-combined",
                    participant_id=pid,
                    project_id=project,
                    application_id=app,
                    environment_id="env",
                    layer="backend",
                    is_primary=pid == "p1",
                    is_traffic_entry=False,
                )
            )
        await session.commit()

    app_only = ConsumerIdentity(
        consumer_id="c2",
        name="c2",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.VIEWER,
        scopes=[ScopeRef(project_id="proj-1", app_id="app-a")],
    )
    total, tests = await list_prompt_usage("sys", app_only, limit=10)
    assert (total, [test.thread_id for test in tests]) == (1, ["thr-app"])
    both = _identity(["proj-1", "proj-2"])
    total, tests = await list_prompt_usage("sys", both, limit=10)
    assert {test.thread_id for test in tests} == {"thr-project", "thr-app", "thr-combined"}
    assert total == 3


async def test_a_slow_index_never_holds_the_launch(database, monkeypatch) -> None:
    import asyncio

    async def stall(*_args, **_kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(prompt_usage, "RECORD_TIMEOUT_S", 0.05)
    monkeypatch.setattr(prompt_usage, "_write_usage", stall)
    await asyncio.wait_for(record_prompt_usage("thr-1", _configurable()), timeout=1)


async def test_the_facade_records_usage_once_the_run_exists(monkeypatch) -> None:
    calls: list[tuple[Any, ...]] = []

    async def record(thread_id, configurable, *, title=None):
        calls.append((thread_id, configurable.get("prompt_manifest"), title))

    monkeypatch.setattr(pipeline_read, "record_prompt_usage", record)
    manifest = _configurable()["prompt_manifest"]

    async def prepare(configurable: dict[str, Any], _input: dict[str, Any]) -> None:
        configurable["prompt_manifest"] = manifest

    client = FakeClient()
    await PipelineReadService(client).start_run(
        title="Recorded", phases=["story_analysis"], principal_id="op", prepare_configurable=prepare
    )
    assert calls == [("thr-1", manifest, "Recorded")]


async def test_a_refused_launch_records_nothing(monkeypatch) -> None:
    calls: list[str] = []

    async def record(thread_id, configurable, *, title=None):
        calls.append(thread_id)

    async def refuse(configurable, *, thread_id, actor_id, resume=False):
        raise HTTPException(409, "Model gpt-4o is archived; select an active model")

    monkeypatch.setattr(pipeline_read, "record_prompt_usage", record)
    monkeypatch.setattr(pipeline_read, "admit_models", refuse)
    with pytest.raises(pipeline_read.ModelAdmissionError):
        await PipelineReadService(FakeClient()).start_run(title="Refused", principal_id="op")
    assert calls == []


async def test_the_usage_route_hides_prompts_the_caller_cannot_read(database) -> None:
    from apex.routers.prompts import get_prompt_usage
    from apex.services.prompts import PromptNotFoundError

    await record_prompt_usage("thr-1", _configurable(), title="Visible")

    class Catalog:
        async def get_prompt(self, prompt_id, *, allow_application):
            if prompt_id != "sys":
                raise PromptNotFoundError(prompt_id)
            return SimpleNamespace(), None

    usage = await get_prompt_usage("sys", _identity(["proj-1"]), cast(Any, Catalog()), limit=5)
    assert usage.total_tests == 1
    assert usage.tests[0].title == "Visible"
    assert usage.tests[0].slot == "system"
    with pytest.raises(HTTPException) as missing:
        await get_prompt_usage("hidden", _identity(["proj-1"]), cast(Any, Catalog()), limit=5)
    assert missing.value.status_code == 404
