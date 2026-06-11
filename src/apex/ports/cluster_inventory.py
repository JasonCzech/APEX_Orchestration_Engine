"""Cluster inventory port (k8s environment scanning / stub)."""

from typing import Protocol, runtime_checkable

from apex.domain.integrations import EnvironmentSnapshot, EnvRef


@runtime_checkable
class ClusterInventoryPort(Protocol):
    async def scan_environment(self, env_ref: EnvRef) -> EnvironmentSnapshot: ...
