"""Selection, tenant visibility, freezing and review invariants over real SQL queries."""

from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.pipeline import Phase
from apex.domain.prompt_selection import PromptApplicability, PromptSelection, PromptSelectionsV1
from apex.graphs.pipeline.configurable import PipelineConfigurable, PromptOverride
from apex.persistence.models import (
    Application,
    Base,
    Environment,
)
from apex.persistence.repositories.prompts import PromptRepository
from apex.services.prompt_selections import (
    edited_review_metadata,
    prepare_prompt_manifest,
    resolve_manifest,
)
from apex.services.prompts import PromptCatalogService, prompt_review_from_resolved


@pytest.fixture
async def db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://", execution_options={"schema_translate_map": {"apex": None}}
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(
                sync,
                tables=[
                    Base.metadata.tables[f"apex.{name}"]
                    for name in (
                        "applications",
                        "project_applications",
                        "environments",
                        "environment_hosts",
                        "environment_snapshots",
                        "prompts",
                        "prompt_versions",
                    )
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("apex.services.prompt_selections.get_sessionmaker", lambda: factory)
    async with factory() as session:
        session.add_all(
            [
                Application(id="app-one", name="One", project_ids=["project-one"]),
                Application(id="app-two", name="Two", project_ids=["project-two"]),
            ]
        )
        await session.commit()
        session.add_all(
            [
                Environment(id="env-one", name="Perf", application_id="app-one"),
                Environment(id="env-two", name="Perf", application_id="app-two"),
            ]
        )
        await session.commit()
        yield session
    await engine.dispose()


def identity(admin=False):
    return ConsumerIdentity(
        consumer_id="tester",
        name="Tester",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.ADMIN if admin else Role.OPERATOR,
        scopes=[] if admin else [ScopeRef(project_id="project-one", app_id="app-one")],
    )


async def create(session, namespace, key, **metadata):
    return await PromptCatalogService(PromptRepository(session)).create_prompt(
        namespace=namespace, key=key, content=f"{namespace}: {key}", applicability=metadata
    )


@pytest.mark.parametrize(
    "metadata",
    [
        {"namespace": "phase"},
        {"namespace": "system", "phase": "reporting"},
        {"namespace": "application"},
        {"namespace": "application", "application_id": "app-one", "is_shared": True},
        {"namespace": "environment"},
        {"namespace": "environment", "environment_id": "env-one", "is_default": True},
    ],
)
def test_invalid_applicability(metadata):
    with pytest.raises(ValidationError):
        PromptApplicability(**metadata)


async def test_defaults_and_archive_are_atomic(db):
    first, _ = await create(db, "system", "first")
    second, _ = await create(db, "system", "second")
    service = PromptCatalogService(PromptRepository(db))
    await service.set_default(first.id)
    with pytest.raises(ValueError, match="replacement"):
        await service.set_archived(first.id, True)
    await service.set_default(second.id)
    await db.refresh(first)
    assert not first.is_default and second.is_default
    await service.set_archived(first.id, True)
    assert first.archived_at is not None


async def test_scoped_catalog_filters_before_pagination_and_detail(db):
    shared, _ = await create(db, "application", "shared", is_shared=True)
    own, _ = await create(db, "application", "own", application_id="app-one")
    hidden, _ = await create(db, "application", "aaa-hidden", application_id="app-two")
    own_env, _ = await create(db, "environment", "own-env", environment_id="env-one")
    hidden_env, _ = await create(db, "environment", "hidden-env", environment_id="env-two")
    repo = PromptRepository(db, identity=identity())
    rows = await repo.search(
        namespace="application",
        application_id="app-one",
        include_shared=True,
        selection_enabled=True,
        limit=1,
    )
    assert [row.id for row in rows] == [own.id]
    rows = await repo.search(
        namespace="application", application_id="app-one", include_shared=True, offset=1
    )
    assert [row.id for row in rows] == [shared.id]
    assert await repo.get(hidden.id) is None
    assert await repo.get(hidden_env.id) is None
    found = await repo.get(own_env.id)
    assert found is not None and found.id == own_env.id


async def test_manifest_is_pinned_ordered_and_system_locked(db):
    system, sv = await create(db, "system", "default")
    phase, pv = await create(db, "phase", "analysis", phase="story_analysis")
    app, av = await create(db, "application", "shared", is_shared=True)
    env, ev = await create(db, "environment", "perf", environment_id="env-one")

    def reference(prompt, version):
        return {"prompt_id": prompt.id, "version_id": version.id}

    config = {
        "phases": ["story_analysis"],
        "project_id": "project-one",
        "app_id": "app-one",
        "environment_id": "env-one",
        "prompt_selections": {
            "schema_version": 1,
            "system": reference(system, sv),
            "phases": {"story_analysis": reference(phase, pv)},
            "application": [reference(app, av)],
            "environment": [reference(env, ev)],
        },
    }
    await prepare_prompt_manifest(config, identity())
    cfg = PipelineConfigurable.model_validate(config)
    from typing import cast

    from langchain_core.runnables import RunnableConfig

    from apex.graphs.pipeline.graph import plan_resolver
    from apex.graphs.pipeline.phase_subgraph import (
        _compose_system,
        _compose_user,
        _validated_prompt_review,
    )
    from apex.graphs.pipeline.state import PipelineState

    state = cast(PipelineState, {"title": "Pinned test", "request": "Analyze checkout"})
    runtime = cast(RunnableConfig, {"configurable": {**config, "thread_id": "test-manifest"}})
    update = plan_resolver(state, runtime)
    assert update["run_config"]["prompt_manifest"] == config["prompt_manifest"]
    review_state, resolved_state = _validated_prompt_review(
        update["prompt_reviews"]["story_analysis"]
    )
    assert review_state["system_locked"] is True
    model_system = _compose_system(resolved_state)
    assert (
        model_system.index("SHARED SYSTEM PROMPT")
        < model_system.index("APPLICATION CONTEXT")
        < model_system.index("ENVIRONMENT CONTEXT")
        < model_system.index("PHASE CONTEXT")
    )
    assert "Analyze checkout" in _compose_user(state, Phase.STORY_ANALYSIS, resolved_state, {})

    before = resolve_manifest(Phase.STORY_ANALYSIS, cfg, {})
    service = PromptCatalogService(PromptRepository(db))
    await service.save_version(system.id, content="Changed after launch")
    await service.set_archived(app.id, True)
    assert resolve_manifest(Phase.STORY_ANALYSIS, cfg, {}) == before
    assert "contexts" in before and before["application"] is not None
    assert [entry["namespace"] for entry in before["contexts"]] == ["application", "environment"]
    assert before["application"].index("APPLICATION CONTEXT") < before["application"].index(
        "ENVIRONMENT CONTEXT"
    )
    review: dict[str, Any] = dict(prompt_review_from_resolved(before))
    with pytest.raises(ValueError, match="locked"):
        edited_review_metadata(review, "changed system", review["application"])
    changed = review["application"].replace("environment: perf", "phase-local environment context")
    updated = edited_review_metadata(review, review["system"], changed)
    assert updated["contexts"][1]["content"] == "phase-local environment context"
    assert cfg.prompt_manifest is not None
    assert cfg.prompt_manifest.environment[0].content == "environment: perf"
    with pytest.raises(HTTPException) as failure:
        await prepare_prompt_manifest({**config, "prompt_manifest": None}, identity())
    assert failure.value.status_code == 422


def test_cardinality_and_mixed_contract():
    ref = {"prompt_id": "prompt", "version_id": "version"}
    selection = PromptSelectionsV1.model_validate(
        {"system": ref, "phases": {"story_analysis": ref}}
    )
    with pytest.raises(ValidationError, match="included phases"):
        PipelineConfigurable(phases=[Phase.REPORTING], prompt_selections=selection)
    with pytest.raises(ValidationError, match="combined"):
        PipelineConfigurable(
            phases=[Phase.STORY_ANALYSIS],
            prompt_selections=selection,
            prompt_overrides={"phase/story_analysis": PromptOverride(content="old")},
        )
    with pytest.raises(ValidationError, match="Duplicate"):
        PromptSelectionsV1.model_validate({**selection.model_dump(), "application": [ref, ref]})
    with pytest.raises(ValidationError, match="blank"):
        PromptSelection(**ref, override_content=" ")


def test_no_manifest_never_falls_back_to_builtins():
    from apex.services.prompts import resolve_phase_prompt_no_catalog

    ref = {"prompt_id": "prompt", "version_id": "version"}
    cfg = PipelineConfigurable(
        phases=[Phase.STORY_ANALYSIS],
        prompt_selections=PromptSelectionsV1.model_validate(
            {"system": ref, "phases": {"story_analysis": ref}}
        ),
    )
    with pytest.raises(ValueError, match="manifest"):
        resolve_phase_prompt_no_catalog(Phase.STORY_ANALYSIS, cfg)


async def test_migration_preserves_custom_versions_archives_and_is_repeatable(db):
    import importlib

    from sqlalchemy import select

    from apex.persistence.models import Prompt, PromptVersion

    service = PromptCatalogService(PromptRepository(db))
    canonical, first = await service.create_prompt(
        namespace="phase", key="story_analysis/system", content="Customized canonical content"
    )
    user, _ = await service.create_prompt(
        namespace="phase", key="story_analysis/user", content="Customized request template"
    )
    archived, _ = await service.create_prompt(
        namespace="phase", key="reporting/system", content="Archived custom reporting"
    )
    await service.set_archived(archived.id, True)
    own, _ = await service.create_prompt(namespace="application", key="app-one", content="Own app")
    unknown, _ = await service.create_prompt(
        namespace="application", key="ambiguous", content="Private"
    )
    migration = importlib.import_module(
        "apex.persistence.migrations.versions.0043_prompt_selections"
    )
    await db.run_sync(lambda session: migration.migrate_catalog(session.connection().execute))
    await db.commit()
    await db.refresh(canonical)
    await db.refresh(archived)
    await db.refresh(own)
    assert canonical.is_default and canonical.phase == "story_analysis"
    assert (
        first.content == "Customized canonical content" and canonical.active_version_id == first.id
    )
    assert not user.selection_enabled and not unknown.selection_enabled
    assert archived.archived_at is not None and not archived.is_default
    assert own.application_id == "app-one" and own.selection_enabled
    defaults = list((await db.scalars(select(Prompt).where(Prompt.is_default.is_(True)))).all())
    assert len(defaults) == 8
    before = [
        (p.id, p.active_version_id, p.is_default)
        for p in (await db.scalars(select(Prompt).order_by(Prompt.id))).all()
    ]
    version_ids = list(
        (await db.scalars(select(PromptVersion.id).order_by(PromptVersion.id))).all()
    )
    await db.run_sync(lambda session: migration.migrate_catalog(session.connection().execute))
    await db.commit()
    after = [
        (p.id, p.active_version_id, p.is_default)
        for p in (await db.scalars(select(Prompt).order_by(Prompt.id))).all()
    ]
    assert before == after
    assert version_ids == list(
        (await db.scalars(select(PromptVersion.id).order_by(PromptVersion.id))).all()
    )


def test_structured_context_edits_preserve_membership_and_reject_system_changes():
    from apex.services.prompt_selections import compose_contexts

    context = {
        "namespace": "application",
        "is_shared": True,
        "prompt_id": "app-context",
        "version_id": "v1",
        "version": 1,
        "key": "shared",
        "content": "original",
    }
    current = {"system_locked": True, "system": "locked", "contexts": [context], "selections": []}
    edit = [{"prompt_id": "app-context", "content": "phase-local"}]
    updated = edited_review_metadata(current, "locked", None, edit)
    assert updated["contexts"][0]["content"] == "phase-local"
    assert current["contexts"][0]["content"] == "original"
    assert "phase-local" in compose_contexts(updated["contexts"])
    with pytest.raises(ValueError, match="membership"):
        edited_review_metadata(current, "locked", None, [])
    with pytest.raises(ValueError, match="locked"):
        edited_review_metadata(current, "changed", None, edit)


def test_multiple_context_review_uses_model_budget_with_individual_part_limits():
    from apex.graphs.pipeline.gates import parse_gate_decision
    from apex.services.run_validation import validate_gate_payload

    decision = {
        "action": "modify",
        "prompt": {
            "context_edits": [
                {"prompt_id": f"prompt-{index}", "content": "x" * 30_000} for index in range(4)
            ]
        },
    }
    validate_gate_payload(decision)
    assert parse_gate_decision(decision, ["modify"])["action"] == "modify"
    decision["prompt"]["context_edits"][0]["content"] = "x" * 50_001
    with pytest.raises(ValueError, match="exceeds"):
        validate_gate_payload(decision)


async def test_facade_launch_freezes_a_manifest_the_plan_resolver_accepts(db):
    """The /v1 launch path bypasses the native run hook; its own manifest must run."""
    from typing import cast

    from langchain_core.runnables import RunnableConfig

    from apex.graphs.pipeline.graph import plan_resolver
    from apex.graphs.pipeline.state import PipelineState
    from apex.services.pipeline_read import PipelineReadService
    from tests.unit.test_pipelines_create_run import FakeClient

    system, sv = await create(db, "system", "default")
    phase, pv = await create(db, "phase", "analysis", phase="story_analysis")
    client = FakeClient()

    async def prepare(configurable: dict[str, Any], run_input: dict[str, Any]) -> None:
        await prepare_prompt_manifest(configurable, identity(), run_input)

    await PipelineReadService(client).start_run(
        title="Wizard launch",
        request="Analyze checkout",
        project_id="project-one",
        app_id="app-one",
        phases=["story_analysis"],
        configurable={
            "environment_id": "env-one",
            "test_scope": {
                "schema_version": 1,
                "participants": [
                    {
                        "id": "backend",
                        "layer": "backend",
                        "project_id": "project-one",
                        "application_id": "app-one",
                        "environment_id": "env-one",
                    }
                ],
                "primary_participant_id": "backend",
                "traffic_entry_participant_id": "backend",
            },
            "prompt_selections": {
                "system": {"prompt_id": system.id, "version_id": sv.id},
                "phases": {"story_analysis": {"prompt_id": phase.id, "version_id": pv.id}},
            },
        },
        prepare_configurable=prepare,
    )

    configurable = client.runs.args[3]["configurable"]
    assert configurable["prompt_manifest"]["system"]["version_id"] == sv.id
    PipelineConfigurable.model_validate(configurable)
    state = cast(PipelineState, {"title": "Wizard launch", "request": "Analyze checkout"})
    runtime = cast(RunnableConfig, {"configurable": {**configurable, "thread_id": "t-facade"}})
    update = plan_resolver(state, runtime)
    assert update["run_config"]["prompt_manifest"] == configurable["prompt_manifest"]
