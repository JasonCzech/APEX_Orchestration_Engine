import pytest

from apex.domain.pipeline import PHASE_ORDER, Phase
from apex.graphs.pipeline.configurable import GateMode, PipelineConfigurable


def test_defaults_select_all_phases() -> None:
    cfg = PipelineConfigurable.from_config({"configurable": {}})
    assert cfg.selected_phases() == list(PHASE_ORDER)
    assert cfg.engine == "sim"


def test_explicit_phases_resolve_in_canonical_order() -> None:
    cfg = PipelineConfigurable.from_config(
        {"configurable": {"phases": ["reporting", "env_triage"]}}
    )
    assert cfg.selected_phases() == [Phase.ENV_TRIAGE, Phase.REPORTING]


def test_explicit_empty_phases_resolves_empty() -> None:
    cfg = PipelineConfigurable.from_config({"configurable": {"phases": []}})
    assert cfg.selected_phases() == []


def test_start_stop_range() -> None:
    cfg = PipelineConfigurable.from_config(
        {"configurable": {"start_phase": "env_triage", "stop_after": "execution"}}
    )
    assert cfg.selected_phases() == [
        Phase.ENV_TRIAGE,
        Phase.SCRIPT_SCENARIO,
        Phase.EXECUTION,
    ]


def test_gate_policy_defaults_to_gated() -> None:
    cfg = PipelineConfigurable.from_config(
        {"configurable": {"gates": {"execution": {"prompt_review": "auto"}}}}
    )
    assert cfg.gate_policy(Phase.EXECUTION).prompt_review is GateMode.AUTO
    assert cfg.gate_policy(Phase.EXECUTION).output_review is GateMode.GATED
    assert cfg.gate_policy(Phase.REPORTING).prompt_review is GateMode.GATED


def test_unknown_configurable_keys_ignored() -> None:
    cfg = PipelineConfigurable.from_config(
        {"configurable": {"thread_id": "t", "langgraph_auth_user": object(), "engine": "sim"}}
    )
    assert cfg.engine == "sim"


def test_checkpointed_config_rejects_live_affinity_drift() -> None:
    durable = PipelineConfigurable(
        project_id="project-a",
        app_id="app-a",
        engine="sim",
    ).snapshot()

    with pytest.raises(ValueError, match="does not match"):
        PipelineConfigurable.from_state(
            {"run_config": durable},
            {
                "configurable": {
                    "project_id": "project-a",
                    "app_id": "app-a",
                    "engine": "loadrunner",
                }
            },
        )


@pytest.mark.parametrize("snapshot", [{}, {"engine": "sim"}, "sim"])
def test_checkpointed_config_requires_complete_canonical_snapshot(snapshot: object) -> None:
    with pytest.raises(ValueError, match="invalid|incomplete"):
        PipelineConfigurable.from_state(
            {"run_config": snapshot},
            {"configurable": {}},
        )


def test_checkpointed_config_rejects_numeric_normalization_drift() -> None:
    snapshot = PipelineConfigurable().snapshot()
    snapshot["environment_target_version"] = 1.0

    with pytest.raises(ValueError, match="not canonical"):
        PipelineConfigurable.from_state(
            {"run_config": snapshot},
            {"configurable": {"environment_target_version": 1}},
        )


def test_runtime_config_rejects_mapping_subclass_without_hooks() -> None:
    class HostileConfig(dict[str, object]):
        called = False

        def get(self, *_args: object, **_kwargs: object) -> object:
            self.called = True
            raise AssertionError("config hooks must not execute")

    hostile = HostileConfig(configurable={})
    with pytest.raises(ValueError, match="configuration is invalid"):
        PipelineConfigurable.from_config(hostile)  # type: ignore[arg-type]
    assert hostile.called is False


def test_runtime_config_ignores_non_string_key_without_comparing_it() -> None:
    class HostileKey:
        compared = False

        def __hash__(self) -> int:
            return hash("engine")

        def __eq__(self, _other: object) -> bool:
            self.compared = True
            raise AssertionError("untyped configurable keys must not be compared")

    hostile = HostileKey()
    configurable = {hostile: "loadrunner"}

    parsed = PipelineConfigurable.from_config({"configurable": configurable})  # type: ignore[dict-item]

    assert parsed.engine == "sim"
    assert hostile.compared is False


@pytest.mark.parametrize(
    "configurable",
    [
        {"environment_id": "env-a", "project_id": "project-a"},
        {"environment_id": "env-a", "app_id": "app-a"},
        {"app_id": "app-a"},
    ],
)
def test_runtime_config_rejects_incomplete_application_ownership(
    configurable: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="ownership|requires project_id"):
        PipelineConfigurable.from_config({"configurable": configurable})


@pytest.mark.parametrize(
    "configurable",
    [
        {"prompt_overrides": {"phase/reporting": {"content": "password=legacy-secret"}}},
        {"pre_execution_context": ["Authorization: Bearer legacy-secret"]},
        {"connections": {"execution_engine": "https://user:legacy-secret@example.test"}},
        {"load_test": {"note": "private_key=legacy-secret"}},
    ],
)
def test_legacy_assistant_configuration_cannot_supply_credentials(
    configurable: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="credential material"):
        PipelineConfigurable.from_config({"configurable": configurable})


def test_invalid_runtime_config_never_retains_credential_on_exception_chain() -> None:
    canary = "runtime-config-secret-canary"
    with pytest.raises(ValueError, match="credential material") as raised:
        PipelineConfigurable.from_config(
            {
                "configurable": {
                    "prompt_overrides": {
                        "phase/reporting": {
                            "content": f"api_key={canary}" + "x" * 100_001,
                        }
                    }
                }
            }
        )

    current: BaseException | None = raised.value
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        assert canary not in repr(current)
        current = current.__cause__ or current.__context__


def test_invalid_runtime_config_does_not_retain_noncredential_input_context() -> None:
    canary = "bare-runtime-phase-canary"
    with pytest.raises(ValueError, match="configuration is invalid") as raised:
        PipelineConfigurable.from_config({"configurable": {"phases": [canary]}})

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert canary not in str(raised.value)


def test_invalid_checkpointed_config_never_retains_credential_on_exception_chain() -> None:
    canary = "checkpoint-config-secret-canary"
    snapshot = PipelineConfigurable().snapshot()
    snapshot["engine"] = f"api_key={canary}"

    with pytest.raises(ValueError, match="credential material") as raised:
        PipelineConfigurable.from_state(
            {"run_config": snapshot},
            {"configurable": {}},
        )

    current: BaseException | None = raised.value
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        assert canary not in repr(current)
        current = current.__cause__ or current.__context__


def test_invalid_checkpointed_config_does_not_retain_noncredential_input_context() -> None:
    canary = "bare-checkpoint-phase-canary"
    snapshot = PipelineConfigurable().snapshot()
    snapshot["phases"] = [canary]

    with pytest.raises(ValueError, match="configuration is invalid") as raised:
        PipelineConfigurable.from_state(
            {"run_config": snapshot},
            {"configurable": {}},
        )

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert canary not in str(raised.value)


def test_phase_entry_rejects_plan_that_differs_from_durable_selection() -> None:
    durable = PipelineConfigurable(phases=[Phase.STORY_ANALYSIS])
    state = {
        "run_config": durable.snapshot(),
        "phases_plan": [Phase.EXECUTION.value],
    }

    with pytest.raises(ValueError, match="phase plan does not match"):
        PipelineConfigurable.from_state_for_phase(
            state,
            {"configurable": {"phases": [Phase.STORY_ANALYSIS.value]}},
            Phase.EXECUTION,
        )
