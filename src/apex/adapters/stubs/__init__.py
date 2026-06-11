"""Deterministic stub adapters — production code (demos, offline dev, integration tests).

Importing this package registers every stub provider with the AdapterRegistry.
"""

from apex.adapters.stubs.artifact_store import MemoryArtifactStore
from apex.adapters.stubs.cluster_inventory import StubClusterInventoryAdapter
from apex.adapters.stubs.documents import StubDocumentsAdapter
from apex.adapters.stubs.log_search import StubLogSearchAdapter
from apex.adapters.stubs.observability import StubObservabilityAdapter
from apex.adapters.stubs.secrets import EnvSecretsAdapter
from apex.adapters.stubs.source_control import StubSourceControlAdapter
from apex.adapters.stubs.work_tracking import StubWorkTrackingAdapter

__all__ = [
    "EnvSecretsAdapter",
    "MemoryArtifactStore",
    "StubClusterInventoryAdapter",
    "StubDocumentsAdapter",
    "StubLogSearchAdapter",
    "StubObservabilityAdapter",
    "StubSourceControlAdapter",
    "StubWorkTrackingAdapter",
]
