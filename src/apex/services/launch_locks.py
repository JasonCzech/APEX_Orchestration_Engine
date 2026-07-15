"""Atomic launch serialization for scoped idempotency keys."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apex.adapters.remote_idempotency import remote_create_guard


class LaunchLockManager:
    """Serialize launches across request services, loops, and production replicas."""

    @asynccontextmanager
    async def hold(self, scope: str) -> AsyncIterator[None]:
        # The shared remote-create guard uses a process-wide threading lock that
        # is safe across short-lived event loops. Locked-down deployments also
        # enable its PostgreSQL advisory lock, preserving cross-replica safety.
        async with remote_create_guard(f"pipeline-launch:{scope}"):
            yield
