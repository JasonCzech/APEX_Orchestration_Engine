"""Run → preset provenance is written once, best-effort, and never fails a launch."""

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.models import Base, RunPresetLink, TestPreset
from apex.services import preset_usage
from apex.services.preset_usage import PresetProvenance, record_preset_usage

LAUNCHER = ConsumerIdentity(
    consumer_id="op-a",
    name="op-a",
    consumer_type=ConsumerType.DASHBOARD,
    role=Role.OPERATOR,
    scopes=[ScopeRef(project_id="proj-a")],
)


@pytest.fixture
async def maker(monkeypatch: pytest.MonkeyPatch) -> Any:
    engine = create_async_engine(
        "sqlite+aiosqlite://", execution_options={"schema_translate_map": {"apex": None}}
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(
                sync,
                tables=[
                    Base.metadata.tables["apex.run_preset_links"],
                    Base.metadata.tables["apex.test_presets"],
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(preset_usage, "get_sessionmaker", lambda: factory)
    yield factory
    await engine.dispose()


async def test_records_the_preset_once_per_test(maker: Any) -> None:
    preset = PresetProvenance(kind="organization", id="asst-release", version=4, name="ACC")
    config = {"project_id": "proj-a", "app_id": "app-a"}
    await record_preset_usage("thr-1", preset, config, LAUNCHER)
    # An idempotent replay of the same launch does not add or change the row.
    await record_preset_usage("thr-1", preset.model_copy(update={"version": 5}), config, LAUNCHER)
    async with maker() as session:
        rows = (await session.scalars(select(RunPresetLink))).all()
    assert [
        (r.thread_id, r.preset_kind, r.preset_id, r.preset_version, r.project_id) for r in rows
    ] == [("thr-1", "organization", "asst-release", 4, "proj-a")]


async def test_nothing_is_recorded_without_a_preset_and_failures_are_swallowed(
    maker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await record_preset_usage("thr-2", None, {}, LAUNCHER)

    def broken() -> Any:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(preset_usage, "get_sessionmaker", broken)
    await record_preset_usage("thr-3", PresetProvenance(kind="team", id="p"), {}, LAUNCHER)
    async with maker() as session:
        assert (await session.scalars(select(RunPresetLink))).all() == []


async def _links(maker: Any) -> list[tuple[str, str, int | None, str | None]]:
    async with maker() as session:
        rows = (
            await session.scalars(select(RunPresetLink).order_by(RunPresetLink.thread_id))
        ).all()
    return [(r.thread_id, r.preset_id, r.preset_version, r.preset_name) for r in rows]


async def test_team_provenance_is_checked_against_the_stored_preset(maker: Any) -> None:
    async with maker() as session:
        for preset_id, project, visibility, owner in [
            ("shared", "proj-a", "project", "someone"),
            ("private", "proj-a", "personal", "someone"),
            ("elsewhere", "proj-z", "project", "someone"),
        ]:
            session.add(
                TestPreset(
                    id=preset_id,
                    name=f"Stored {preset_id}",
                    project_id=project,
                    visibility=visibility,
                    payload={"schema_version": 1, "sections": {}},
                    version=3,
                    created_by=owner,
                    created_by_consumer_id=owner,
                )
            )
        await session.commit()

    def claim(preset_id: str, **extra: Any) -> PresetProvenance:
        return PresetProvenance(kind="team", id=preset_id, name="Claimed name", **extra)

    # The stored name wins and a claimed future version is clamped to the stored one.
    await record_preset_usage("thr-a", claim("shared", version=9), {}, LAUNCHER)
    # Unknown presets, someone else's personal preset and other projects' presets
    # are not recorded, so nobody can inflate their usage or name a run after them.
    for thread, preset_id in [("thr-b", "missing"), ("thr-c", "private"), ("thr-d", "elsewhere")]:
        await record_preset_usage(thread, claim(preset_id), {}, LAUNCHER)
    # Organization names are kept, unless they look like credential material.
    secret = PresetProvenance(
        kind="organization", id="asst", name="Authorization: Bearer abcdefghijklmnop"
    )
    await record_preset_usage("thr-e", secret, {}, LAUNCHER)
    assert await _links(maker) == [
        ("thr-a", "shared", 3, "Stored shared"),
        ("thr-e", "asst", None, None),
    ]
