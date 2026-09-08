"""Prompt integration regressions: missing inputs, handoffs and accepted output."""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig

from apex.domain.agent_deliverables import output_contract, parse_deliverable
from apex.domain.pipeline import PHASE_ORDER, Phase
from apex.domain.prompt_catalog import DEFAULT_PHASE_PROMPTS, PHASE_SECTIONS, PLATFORM_POLICY
from apex.graphs.pipeline import phase_subgraph as ps
from apex.graphs.pipeline.execution_phase import _build_spec
from apex.services.prompts import PromptTemplateError, render_catalog_template


def document(for_phase: Phase = Phase.STORY_ANALYSIS, /, **changes: Any) -> str:
    data: dict[str, Any] = {
        "schema_version": 1,
        "phase": for_phase.value,
        "disposition": "complete",
        "summary": "Reviewed supplied evidence.",
        "sections": {key: "Evidence: requirement R1." for key in PHASE_SECTIONS[for_phase]},
        "evidence": ["requirement:R1"],
        "assumptions": [],
        "gaps": [],
        "blockers": [],
        "workload": [{"scenario_id": "S1", "percent": 100}]
        if for_phase is Phase.TEST_PLANNING
        else [],
    }
    return json.dumps({**data, **changes})


@pytest.mark.parametrize("phase", PHASE_ORDER)
def test_all_defaults_render_and_have_a_matching_output_contract(phase: Phase) -> None:
    for part in ("system", "user"):
        render_catalog_template(
            DEFAULT_PHASE_PROMPTS[f"{phase.value}/{part}"], {"title": "T", "request": "R"}
        )
    assert parse_deliverable(document(phase), phase).phase == phase.value
    assert phase.value in output_contract(phase)


def test_missing_catalog_variable_fails_without_echoing_input() -> None:
    with pytest.raises(PromptTemplateError, match="story_context"):
        render_catalog_template("Stories: {story_context}", {"title": "private user input"})


def test_catalog_json_examples_render_without_reinterpreting_user_braces() -> None:
    text = 'Request: {request}\nExample: {"target": null}\nLiteral: {{name}}'
    assert render_catalog_template(text, {"request": "keep {literal}"}) == (
        'Request: keep {literal}\nExample: {"target": null}\nLiteral: {{name}}'
    )


def test_catalog_template_uses_exact_rendered_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "apex.services.prompts.get_settings",
        lambda: SimpleNamespace(runs=SimpleNamespace(max_model_input_chars=10_000)),
    )
    assert len(render_catalog_template("{request}{request}", {"request": "x" * 5_000})) == 10_000
    with pytest.raises(PromptTemplateError, match="limit"):
        render_catalog_template("{request}{request}!", {"request": "x" * 5_000})


@pytest.mark.parametrize("field", ["{request.value}", "{request[0]}", "{request:>9}", "{request"])
def test_catalog_rejects_unsupported_fields_even_after_json(field: str) -> None:
    with pytest.raises(PromptTemplateError, match="simple"):
        render_catalog_template('{"target": null}\n' + field, {"request": "test"})


@pytest.mark.parametrize(
    "changes",
    [
        {"phase": "reporting"},
        {"sections": {}},
        {"unexpected": True},
        {"disposition": "complete", "blockers": ["missing target"]},
        {"disposition": "blocked", "blockers": []},
        {"summary": "   "},
    ],
)
def test_invalid_output_cannot_be_accepted(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_deliverable(document(**changes), Phase.STORY_ANALYSIS)


@pytest.mark.parametrize(
    "workload",
    [
        [],
        [{"scenario_id": "S1", "percent": 60}],
        [{"scenario_id": "S1", "percent": 50}, {"scenario_id": "S1", "percent": 50}],
        [{"scenario_id": "S1", "percent": 100}, {"scenario_id": "S2", "percent": 0}],
    ],
)
def test_complete_plan_requires_consistent_workload(workload: list[dict[str, Any]]) -> None:
    with pytest.raises(ValueError):
        parse_deliverable(document(Phase.TEST_PLANNING, workload=workload), Phase.TEST_PLANNING)


def test_blocked_plan_can_preserve_partial_work() -> None:
    result = parse_deliverable(
        document(
            Phase.TEST_PLANNING,
            disposition="blocked",
            blockers=["Traffic mix is unknown"],
            workload=[],
        ),
        Phase.TEST_PLANNING,
    )
    assert result.disposition == "blocked"


def test_provider_output_is_lossless_or_rejected() -> None:
    raw = document()
    assert ps._message_deliverable_text(SimpleNamespace(content=raw)) == raw
    with pytest.raises(ValueError, match="size limit"):
        ps._message_deliverable_text(SimpleNamespace(content="x" * 20_001))
    with pytest.raises(ValueError):
        parse_deliverable(raw[:-1] + ',"phase":"reporting"}', Phase.STORY_ANALYSIS)


def test_model_text_blocks_preserve_json_split_inside_a_string() -> None:
    raw = document()
    split = raw.index("Reviewed") + 3
    response = SimpleNamespace(
        content=[
            {"type": "text", "text": raw[:split]},
            {"type": "thinking", "thinking": "not output"},
            {"type": "text", "text": raw[split:]},
        ]
    )
    assert ps._message_deliverable_text(response) == raw
    parse_deliverable(ps._message_deliverable_text(response), Phase.STORY_ANALYSIS)


def test_platform_policy_survives_overrides_and_evidence_is_not_promoted() -> None:
    system = ps._compose_system(
        {"system": "Phase preference", "application": "Application preference"}
    )
    assert system.startswith(PLATFORM_POLICY)
    assert system.index("=== PHASE INSTRUCTIONS") < system.index("=== APPLICATION CONTEXT")
    assert "Application preference" in system


def test_handoff_carries_accepted_deliverable_and_explicit_approval_status() -> None:
    state: Any = {
        "phase_results": {
            "story_analysis": {
                "status": "succeeded",
                "attempt": 2,
                "summary": "Story coverage",
                "deliverable_json": document(),
                "approvals": [],
            }
        }
    }
    handoff = ps._upstream_handoff(state, Phase.TEST_PLANNING)
    data = json.loads(handoff.split("\n", 1)[1])[0]
    assert data["attempt"] == 2 and data["accepted"] is True
    assert data["deliverable"]["evidence"] == ["requirement:R1"]
    assert len(data["content_sha256"]) == 64
    assert "no human" in data["approval"]
    state["phase_results"]["story_analysis"]["status"] = "failed"
    assert (
        "deliverable"
        not in json.loads(ps._upstream_handoff(state, Phase.TEST_PLANNING).split("\n", 1)[1])[0]
    )


def test_model_script_never_silently_executes_default_workload() -> None:
    with pytest.raises(ValueError, match="draft artifacts"):
        _build_spec(
            {"phase_results": {"script_scenario": {"provenance": "llm"}}},
            {"configurable": {"thread_id": "draft-script"}},
            1,
            "apex_load",
        )


def test_blocked_deliverable_cannot_be_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    decisions = iter([{"action": "approve"}, {"action": "abort"}])
    payloads: list[dict[str, Any]] = []

    def decide(payload: dict[str, Any]) -> dict[str, Any]:
        payloads.append(payload)
        return next(decisions)

    monkeypatch.setattr(ps, "interrupt", decide)
    state: Any = {
        "phase_results": {
            "story_analysis": {
                "attempt": 1,
                "status": "awaiting_output_review",
                "summary": "Missing scope",
                "deliverable_json": document(disposition="blocked", blockers=["Missing scope"]),
            }
        }
    }
    config: RunnableConfig = {"configurable": {"thread_id": "blocked-review"}}
    command = ps._make_output_gate(Phase.STORY_ANALYSIS)(state, config)
    assert len(payloads) == 2
    assert isinstance(command.update, dict)
    assert command.update["phase_results"]["story_analysis"]["status"] == "aborted"


async def test_full_deliverable_is_persisted_with_content_addressed_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apex.ports.artifact_store import StoredArtifact

    writes: list[dict[str, Any]] = []
    closed: list[bool] = []

    class Resolver:
        async def resolve_with_connection_id(self, *args: Any, **kwargs: Any) -> tuple[Any, str]:
            return object(), "artifact-connection"

    async def persist(store: Any, **kwargs: Any) -> StoredArtifact:
        writes.append(kwargs)
        return StoredArtifact(
            key=kwargs["artifact_key"], uri="memory://deliverable", size=len(kwargs["payload"])
        )

    async def close(*args: Any) -> None:
        closed.append(True)

    monkeypatch.setattr(ps, "_make_artifact_resolver", Resolver)
    monkeypatch.setattr(ps, "persist_artifact_with_intent", persist)
    monkeypatch.setattr(ps, "_close_transcript_resources_definitively", close)
    raw = document()
    config: RunnableConfig = {"configurable": {"thread_id": "artifact-test"}}
    first = await ps._persist_agent_deliverable({}, Phase.STORY_ANALYSIS, config, 1, raw)
    second = await ps._persist_agent_deliverable({}, Phase.STORY_ANALYSIS, config, 1, raw)
    changed = await ps._persist_agent_deliverable(
        {}, Phase.STORY_ANALYSIS, config, 1, document(summary="Revised summary")
    )
    assert writes[0]["payload"].decode() == raw
    assert first["key"] == second["key"] != changed["key"]
    assert first["artifact_connection_id"] == "artifact-connection"
    assert len(closed) == 3

    async def broken_close(*args: Any) -> None:
        raise OSError("cleanup failure")

    monkeypatch.setattr(ps, "_close_transcript_resources_definitively", broken_close)
    committed = await ps._persist_agent_deliverable({}, Phase.STORY_ANALYSIS, config, 1, raw)
    assert committed["key"] == first["key"]


def test_escaped_invalid_text_and_boolean_schema_are_rejected() -> None:
    for changes in ({"schema_version": True}, {"summary": "has\x00nul"}, {"blockers": [" "]}):
        with pytest.raises(ValueError):
            parse_deliverable(document(**changes), Phase.STORY_ANALYSIS)


@pytest.mark.parametrize("concurrent_edit", [False, True])
async def test_seed_upgrade_preserves_custom_and_archived_prompts(
    monkeypatch: pytest.MonkeyPatch,
    concurrent_edit: bool,
) -> None:
    from scripts import seed_prompts

    texts = {
        "story_analysis/system": (
            "You are the APEX story analysis agent. "
            "Produce the story_analysis deliverable for this performance-testing run."
        ),
        "reporting/system": "Organization-specific reporting instructions",
        "postmortem/system": None,  # archived prompts have no active resolver version
    }
    saved: list[str] = []

    class Session:
        async def refresh(self, existing: Any, *, with_for_update: bool) -> None:
            assert with_for_update is True
            if concurrent_edit and existing.id == "story_analysis/system":
                texts[existing.id] = "Concurrent operator customization"

        async def rollback(self) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

    class Repository:
        def __init__(self, session: Any) -> None:
            pass

        async def get_by_key(self, namespace: str, key: str) -> Any:
            return SimpleNamespace(id=key)

        async def get_active_version(self, namespace: str, key: str) -> Any:
            return SimpleNamespace(content=texts[key]) if texts[key] is not None else None

    class Catalog:
        def __init__(self, repo: Any) -> None:
            pass

        async def save_version(self, key: str, *, content: str, **kwargs: Any) -> Any:
            saved.append(key)
            texts[key] = content
            return None, SimpleNamespace(version=2)

    monkeypatch.setattr(seed_prompts, "get_sessionmaker", lambda: Session)
    monkeypatch.setattr(seed_prompts, "PromptRepository", Repository)
    monkeypatch.setattr(seed_prompts, "PromptCatalogService", Catalog)
    monkeypatch.setattr(
        seed_prompts, "DEFAULT_PHASE_PROMPTS", {key: DEFAULT_PHASE_PROMPTS[key] for key in texts}
    )
    assert await seed_prompts.main(upgrade_defaults=True) == 0
    assert await seed_prompts.main(upgrade_defaults=True) == 0
    assert saved == ([] if concurrent_edit else ["story_analysis/system"])
    assert texts["reporting/system"] == "Organization-specific reporting instructions"
    assert texts["postmortem/system"] is None


def test_generated_dashboard_catalog_matches_backend() -> None:
    from scripts.export_prompt_catalog import POLICY_TARGET, TARGET, render, render_policy

    assert TARGET.read_text() == render()
    assert POLICY_TARGET.read_text() == render_policy()
