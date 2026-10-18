"""Metadata-only checks for an explicitly selected pipeline execution contract.

These checks describe the current connection selection, not provider health or a
reservation of that selection. Execution still pins and validates its connection.
"""

from datetime import datetime

from pydantic import BaseModel

from apex.adapters.registry import PortKind
from apex.domain.pipeline import Phase
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.services.connections import get_connection_resolver


class ExecutionReadiness(BaseModel):
    execution_selected: bool
    configuration_valid: bool
    requires_application: bool
    detail: str | None = None


class LaunchReadinessError(ValueError):
    """A new launch cannot satisfy a server-owned connection requirement."""


async def execution_readiness(config: PipelineConfigurable) -> ExecutionReadiness:
    if Phase.EXECUTION not in config.selected_phases():
        return ExecutionReadiness(
            execution_selected=False, configuration_valid=True, requires_application=False
        )
    metadata = None
    artifact_metadata = None
    try:
        resolver = get_connection_resolver()
        metadata = await resolver.resolve_metadata(
            PortKind.EXECUTION_ENGINE,
            connection_id=config.connections.get(PortKind.EXECUTION_ENGINE.value),
            project_id=config.project_id,
            expected_provider=config.engine,
        )
        artifact_metadata = await resolver.resolve_metadata(
            PortKind.ARTIFACT_STORE,
            connection_id=config.connections.get(PortKind.ARTIFACT_STORE.value),
            project_id=config.project_id,
        )
    except Exception:  # Deliberately detach provider/store details from public copy.
        pass
    if any(
        selected is None
        or type(selected.persisted) is not bool
        or (selected.persisted and type(selected.connection_version) is not datetime)
        for selected in (metadata, artifact_metadata)
    ):
        return ExecutionReadiness(
            execution_selected=True,
            configuration_valid=False,
            requires_application=False,
            detail=(
                "Execution and artifact connections could not be verified for this project "
                "and engine. "
                "Ask an administrator to check its connection configuration."
            ),
        )
    assert metadata is not None and artifact_metadata is not None
    requires_application = metadata.persisted or artifact_metadata.persisted
    missing_scope = requires_application and (not config.project_id or not config.app_id)
    return ExecutionReadiness(
        execution_selected=True,
        configuration_valid=not missing_scope,
        requires_application=requires_application,
        detail=(
            "Select a project and application before execution. "
            "The configured engine or artifact store requires both to record its run and results."
            if missing_scope
            else None
        ),
    )


async def require_execution_readiness(config: PipelineConfigurable) -> None:
    readiness = await execution_readiness(config)
    if not readiness.configuration_valid:
        raise LaunchReadinessError(readiness.detail)
