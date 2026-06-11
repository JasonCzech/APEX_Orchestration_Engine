"""Integration ports: async, @runtime_checkable typing.Protocol per ADR-0002.

Graph nodes, services, and routers depend on these protocols only; concrete
providers live under apex.adapters and are resolved through the AdapterRegistry.
"""

from apex.ports.artifact_store import ArtifactStorePort, StoredArtifact
from apex.ports.cluster_inventory import ClusterInventoryPort
from apex.ports.documents import DocumentRetrievalPort
from apex.ports.execution_engine import (
    TERMINAL_ENGINE_PHASES,
    EngineRunPhase,
    EngineRunStatus,
    ExecutionEnginePort,
    LiveStats,
)
from apex.ports.log_search import LogSearchPort
from apex.ports.observability import ObservabilityPort
from apex.ports.secrets import SecretsPort
from apex.ports.source_control import SourceControlPort
from apex.ports.work_tracking import WorkTrackingPort

__all__ = [
    "TERMINAL_ENGINE_PHASES",
    "ArtifactStorePort",
    "ClusterInventoryPort",
    "DocumentRetrievalPort",
    "EngineRunPhase",
    "EngineRunStatus",
    "ExecutionEnginePort",
    "LiveStats",
    "LogSearchPort",
    "ObservabilityPort",
    "SecretsPort",
    "SourceControlPort",
    "StoredArtifact",
    "WorkTrackingPort",
]
