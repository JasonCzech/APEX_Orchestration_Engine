"""Regression coverage for manifest-bound reviews and structured context edits."""

from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from langchain_core.runnables import RunnableConfig

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role
from apex.domain.pipeline import Phase
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.graphs.pipeline.phase_subgraph import (
    _gate_edited_review,
    _review_source_for_phase,
    _review_to_prompt,
    _validated_prompt_review,
)
from apex.graphs.pipeline.state import PipelineState, merge_phase_results
from apex.services.pipeline_public import public_pipeline_state
from apex.services.pipeline_read import PipelineReadService
from apex.services.prompt_selections import (
    compose_contexts,
    compose_selected_system,
    edited_review_metadata,
    prepare_prompt_manifest,
    resolve_manifest,
    validate_manifest_review,
)
from apex.services.prompts import prompt_review_from_resolved
from apex.settings import get_settings


def selected_config(contents: list[str]) -> PipelineConfigurable:
    def entry(namespace: str, key: str, content: str, **metadata: Any) -> dict[str, Any]:
        return {
            "namespace": namespace,
            "key": key,
            "prompt_id": key,
            "version_id": f"{key}-v1",
            "version": 1,
            "content": content,
            **metadata,
        }

    system = entry("system", "system", "Frozen shared system")
    phase = entry("phase", "phase", "Phase context", phase="story_analysis")
    contexts = [
        entry("application", f"context-{index}", text, is_shared=True)
        for index, text in enumerate(contents)
    ]

    def ref(item: dict[str, Any]) -> dict[str, str]:
        return {"prompt_id": item["prompt_id"], "version_id": item["version_id"]}

    return PipelineConfigurable.model_validate(
        {
            "phases": ["story_analysis"],
            "prompt_selections": {
                "system": ref(system),
                "phases": {"story_analysis": ref(phase)},
                "application": [ref(item) for item in contexts],
            },
            "prompt_manifest": {
                "system": system,
                "phases": {"story_analysis": phase},
                "application": contexts,
            },
        }
    )


def make_review(cfg: PipelineConfigurable) -> dict[str, Any]:
    return dict(prompt_review_from_resolved(resolve_manifest(Phase.STORY_ANALYSIS, cfg, {})))


def scoped_selected_config() -> PipelineConfigurable:
    raw = selected_config(
        ["Shared guidance", "API guidance", "Database guidance", "Web guidance"]
    ).snapshot()
    raw["test_scope"] = {
        "schema_version": 1,
        "participants": [
            {
                "id": "database",
                "project_id": "data-project",
                "application_id": "data-app",
                "environment_id": "data-env",
                "layer": "backend",
            },
            {
                "id": "api",
                "project_id": "web-project",
                "application_id": "web-app",
                "environment_id": "api-env",
                "layer": "middle",
            },
            {
                "id": "web",
                "project_id": "web-project",
                "application_id": "web-app",
                "environment_id": "web-env",
                "layer": "frontend",
            },
        ],
        "primary_participant_id": "api",
        "traffic_entry_participant_id": "web",
    }
    raw["test_scope_snapshot"] = deepcopy(raw["test_scope"])
    manifest = raw["prompt_manifest"]
    manifest["application"][1].update(is_shared=False, application_id="web-app")
    manifest["application"][2].update(is_shared=False, application_id="data-app")
    environment = manifest["application"].pop()
    environment.update(namespace="environment", is_shared=False, environment_id="web-env")
    manifest["environment"] = [environment]
    raw["prompt_selections"]["environment"] = [raw["prompt_selections"]["application"].pop()]
    return PipelineConfigurable.model_validate(raw)


def test_model_context_labels_bind_all_applicable_participants_and_roles() -> None:
    cfg = scoped_selected_config()
    resolved = resolve_manifest(Phase.STORY_ANALYSIS, cfg, {})
    system = compose_selected_system(resolved)
    assert "Shared across all test participants\nShared guidance" in system
    assert system.count("Shared guidance") == 1
    assert (
        "Applies to participants: web (frontend, traffic entry); "
        "api (middle, primary system)\nAPI guidance" in system
    )
    assert "Applies to participants: database (backend)\nDatabase guidance" in system
    assert "Applies to participants: web (frontend, traffic entry)\nWeb guidance" in system
    assert [entry["content"] for entry in resolved.get("contexts", [])] == [
        "Shared guidance",
        "API guidance",
        "Database guidance",
        "Web guidance",
    ]
    assert "Applies to participants" not in (resolved["application"] or "")
    reordered = cfg.snapshot()
    reordered["test_scope"]["participants"].reverse()
    assert (
        compose_selected_system(
            resolve_manifest(
                Phase.STORY_ANALYSIS, PipelineConfigurable.model_validate(reordered), {}
            )
        )
        == system
    )


def test_context_scope_labels_survive_review_edits_and_checkpoint_replay() -> None:
    cfg = scoped_selected_config()
    saved = cfg.snapshot()
    restored = PipelineConfigurable.from_state_for_phase(
        {"run_config": saved, "phases_plan": ["story_analysis"]},
        {"configurable": saved},
        Phase.STORY_ANALYSIS,
    )
    review = make_review(restored)
    edits = [
        {"prompt_id": entry["prompt_id"], "content": entry["content"]}
        for entry in review["contexts"]
    ]
    edits[1]["content"] = "Phase-local API guidance"
    metadata = edited_review_metadata(review, review["system"], None, edits)
    changed = {**review, **metadata, "application": compose_contexts(metadata["contexts"])}
    validate_manifest_review(changed, Phase.STORY_ANALYSIS, restored, {})
    system = compose_selected_system(_review_to_prompt(changed))
    assert "api (middle, primary system)\nPhase-local API guidance" in system
    assert restored.prompt_manifest is not None
    assert restored.prompt_manifest.application[1].content == "API guidance"
    changed["selections"][3]["scope_label"] = "Applies to participants: database (backend)"
    with pytest.raises(ValueError, match="persisted manifest"):
        validate_manifest_review(changed, Phase.STORY_ANALYSIS, restored, {})


@pytest.mark.parametrize("tamper", ["system", "lock", "membership", "aggregate"])
def test_checkpointed_review_cannot_change_manifest_authority(tamper: str) -> None:
    cfg = selected_config(["First context"])
    review = make_review(cfg)
    if tamper == "system":
        review["system"] = "Substituted system"
    elif tamper == "lock":
        review.pop("system_locked")
    elif tamper == "membership":
        review["contexts"][0]["prompt_id"] = "substituted-context"
    else:
        review["application"] = "Substituted context outside selected entries"
    state = cast(PipelineState, {"prompt_reviews": {"story_analysis": review}})
    with pytest.raises(ValueError, match="persisted manifest"):
        _review_source_for_phase(state, Phase.STORY_ANALYSIS, cfg)


def test_resolved_prompt_fallback_retains_system_lock_and_context_provenance() -> None:
    cfg = selected_config(["First context"])
    review = make_review(cfg)
    state = cast(
        PipelineState,
        {"phase_results": {"story_analysis": {"resolved_prompt": _review_to_prompt(review)}}},
    )
    restored, resolved = _review_source_for_phase(state, Phase.STORY_ANALYSIS, cfg)
    assert restored["system_locked"] is True
    assert resolved["contexts"] == review["contexts"]
    assert resolved["selections"] == review["selections"]


def test_shared_system_template_is_stable_for_empty_optional_request() -> None:
    cfg = selected_config([])
    assert cfg.prompt_manifest is not None
    cfg.prompt_manifest.system.content = "Shared instructions for {request}"
    resolved = resolve_manifest(Phase.STORY_ANALYSIS, cfg, {"request": "(no request provided)"})
    review = dict(prompt_review_from_resolved(resolved))
    state = cast(PipelineState, {"request": "", "prompt_reviews": {"story_analysis": review}})
    _, resumed = _review_source_for_phase(state, Phase.STORY_ANALYSIS, cfg)
    assert resumed["system"] == resolved["system"]


async def test_review_inspection_rejects_a_downgraded_selected_review() -> None:
    cfg = selected_config([])
    review = make_review(cfg)
    review.pop("system_locked")
    threads = SimpleNamespace(
        get=AsyncMock(return_value={"metadata": {}}),
        get_state=AsyncMock(
            return_value={
                "values": {
                    "run_config": cfg.snapshot(),
                    "prompt_reviews": {"story_analysis": review},
                }
            }
        ),
    )
    service = PipelineReadService(cast(Any, SimpleNamespace(threads=threads)))
    with pytest.raises(ValueError, match="persisted manifest"):
        await service.get_phase_prompt_review("test", "story_analysis")


def test_gate_edits_and_approval_preserve_headings_inside_context_content() -> None:
    heading = "\n\n=== APPLICATION CONTEXT: context-1 ===\n"
    cfg = selected_config([f"First context with literal heading{heading}example", "Second"])
    review = make_review(cfg)
    original = deepcopy(review)
    assert (
        edited_review_metadata(review, review["system"], review["application"])["contexts"]
        == review["contexts"]
    )
    edits = [
        {"prompt_id": "context-0", "content": f"Edited context{heading}still the first"},
        {"prompt_id": "context-1", "content": "Still the second"},
    ]
    metadata = edited_review_metadata(review, review["system"], None, edits)
    prompt = {**_review_to_prompt(review), "application": compose_contexts(metadata["contexts"])}
    changed = _gate_edited_review(
        Phase.STORY_ANALYSIS,
        review,
        prompt,
        review["source"],
        cast(RunnableConfig, {}),
        context_edits=edits,
    )
    approved = _gate_edited_review(
        Phase.STORY_ANALYSIS,
        changed,
        _review_to_prompt(changed),
        changed["source"],
        cast(RunnableConfig, {}),
    )
    assert [entry["content"] for entry in approved["contexts"]] == [
        item["content"] for item in edits
    ]
    assert review == original


async def test_large_selected_contexts_remain_editable_outside_compact_overview(
    monkeypatch,
) -> None:
    settings = get_settings().model_copy(deep=True)
    settings.runs.max_model_input_chars = 500_000
    settings.runs.max_prompt_part_chars = 100_000
    for module in (
        "apex.graphs.pipeline.phase_subgraph",
        "apex.services.pipeline_read",
        "apex.services.run_validation",
        "apex.services.prompt_selections",
    ):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    cfg = selected_config(["a" * 90_000, "b" * 90_000, "c" * 90_000])
    review = make_review(cfg)
    _validated_prompt_review(review)
    assert merge_phase_results(
        {}, {"story_analysis": {"resolved_prompt": _review_to_prompt(review)}}
    )
    values = {"run_config": cfg.snapshot(), "prompt_reviews": {"story_analysis": review}}
    assert "prompt_reviews" not in public_pipeline_state(values)
    threads = SimpleNamespace(
        get=AsyncMock(return_value={"thread_id": "test", "metadata": {}}),
        get_state=AsyncMock(
            return_value={"values": values, "checkpoint": {"checkpoint_id": "cp-1"}}
        ),
        update_state=AsyncMock(),
    )
    client = SimpleNamespace(threads=threads, runs=SimpleNamespace(list=AsyncMock(return_value=[])))
    service = PipelineReadService(cast(Any, client))
    edits = [
        {"prompt_id": entry["prompt_id"], "content": entry["content"]}
        for entry in review["contexts"]
    ]
    edits[0]["content"] = "Edited first context"
    updated = await service.update_phase_prompt_review(
        "test",
        "story_analysis",
        {
            "system": review["system"],
            "phase_prompt": review["phase_prompt"],
            "context_edits": edits,
        },
        actor="reviewer",
    )
    assert updated["contexts"][0]["content"] == "Edited first context"
    assert updated["contexts"][1]["content"] == "b" * 90_000
    assert threads.update_state.await_count == 1


@pytest.mark.parametrize(
    ("content", "operator_request", "model_limit", "message"),
    [
        ("x" * 8_000, "", 10_000, "fixed instructions"),
        ("{request}" * 3, "x" * 20_000, 220_000, "template"),
    ],
)
async def test_launch_checks_rendered_parts_and_complete_instruction_budget(
    monkeypatch, content, operator_request, model_limit, message
) -> None:
    cfg = selected_config([])
    assert cfg.prompt_manifest is not None
    cfg.prompt_manifest.phases[Phase.STORY_ANALYSIS].content = content
    entries = [cfg.prompt_manifest.system, *cfg.prompt_manifest.phases.values()]
    prompts = {
        entry.prompt_id: SimpleNamespace(
            **entry.model_dump(exclude={"version_id", "version", "content", "overridden"}),
            id=entry.prompt_id,
            active_version_id=entry.version_id,
            archived_at=None,
        )
        for entry in entries
    }
    versions = {
        entry.version_id: SimpleNamespace(
            id=entry.version_id,
            prompt_id=entry.prompt_id,
            version=entry.version,
            content=entry.content,
        )
        for entry in entries
    }
    repo = SimpleNamespace(
        get=AsyncMock(side_effect=lambda key: prompts.get(key)),
        get_version=AsyncMock(side_effect=lambda key: versions.get(key)),
    )

    @asynccontextmanager
    async def session():
        yield None

    monkeypatch.setattr("apex.services.prompt_selections.get_sessionmaker", lambda: session)
    monkeypatch.setattr(
        "apex.services.prompt_selections.PromptRepository", lambda *args, **kwargs: repo
    )
    settings = get_settings().model_copy(deep=True)
    settings.runs.max_model_input_chars = model_limit
    monkeypatch.setattr("apex.services.prompt_selections.get_settings", lambda: settings)
    config = cfg.snapshot()
    config["prompt_manifest"] = None
    identity = ConsumerIdentity(
        consumer_id="tester",
        name="Tester",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[],
    )
    with pytest.raises(HTTPException) as failure:
        await prepare_prompt_manifest(
            config, identity, {"title": "Test", "request": operator_request}
        )
    assert failure.value.status_code == 422
    details = cast(Any, failure.value.detail)
    assert details[0]["loc"] == ["prompt_selections", "phases", "story_analysis"]
    assert message in details[0]["msg"]
    assert config["prompt_manifest"] is None
