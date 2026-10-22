"""Only the active plan's reporting phase requires analysis connection readiness."""

from unittest.mock import AsyncMock

import pytest

from apex.domain.pipeline import PHASE_ORDER, Phase
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.services import analysis_readiness


@pytest.mark.parametrize("owner", ["langgraph", "hitl"])
async def test_nonreporting_owned_rerun_does_not_resolve_unrelated_tools(monkeypatch, owner):
    cfg = PipelineConfigurable.model_validate(
        {
            "engine": "loadrunner",
            "phase_execution": {
                "schema_version": 1,
                "owners": {p.value: owner for p in PHASE_ORDER},
            },
            "rerun_phases": [Phase.STORY_ANALYSIS.value],
            "analysis": {"selections": [{"tool": "perfreportforge", "connection_id": "removed"}]},
        }
    )

    def forbidden():
        raise AssertionError("Unselected reporting phase resolved a connection")

    monkeypatch.setattr(analysis_readiness, "get_connection_resolver", forbidden)
    await analysis_readiness.require_analysis_readiness(cfg)


async def test_reporting_rerun_still_requires_selected_connection(monkeypatch):
    cfg = PipelineConfigurable.model_validate(
        {
            "engine": "loadrunner",
            "phase_execution": {
                "schema_version": 1,
                "owners": {p.value: "langgraph" for p in PHASE_ORDER},
            },
            "rerun_phases": [Phase.REPORTING.value],
            "analysis": {"selections": [{"tool": "perfreportforge", "connection_id": "removed"}]},
        }
    )
    resolver = AsyncMock()
    resolver.resolve_metadata.side_effect = KeyError("removed")
    monkeypatch.setattr(analysis_readiness, "get_connection_resolver", lambda: resolver)
    with pytest.raises(ValueError, match="unavailable"):
        await analysis_readiness.require_analysis_readiness(cfg)
    assert resolver.resolve_metadata.call_count == 1
