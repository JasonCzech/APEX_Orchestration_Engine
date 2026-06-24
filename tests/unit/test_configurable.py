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
