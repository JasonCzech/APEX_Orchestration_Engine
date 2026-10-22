"""Validate explicit analysis connections before a new pipeline can start."""

from apex.adapters.registry import PortKind
from apex.domain.pipeline import Phase
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.services.connections import get_connection_resolver


async def require_analysis_readiness(config: PipelineConfigurable) -> None:
    if not config.analysis or Phase.REPORTING not in config.planned_phases():
        return
    resolver = get_connection_resolver()
    for selected in config.analysis.selections:
        try:
            await resolver.resolve_metadata(
                PortKind.EXECUTION_ENGINE if selected.tool == "internal_rca" else PortKind.ANALYSIS,
                connection_id=selected.connection_id,
                project_id=config.traffic_project_id
                if selected.tool == "internal_rca"
                else config.project_id,
                expected_provider="apex_load" if selected.tool == "internal_rca" else selected.tool,
            )
        except (KeyError, ValueError) as exc:
            raise ValueError("Selected analysis connection is unavailable or incompatible") from exc
