"""Async repository for the ApiConsumer aggregate (consumer + scopes).

Raw API keys are never persisted: callers hash with `apex.auth.service.hash_api_key`
before handing the digest to this repository.
"""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apex.auth.identity import ScopeRef
from apex.persistence.models import ApiConsumer, ConsumerScope


class ConsumersRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_all(self) -> list[ApiConsumer]:
        result = await self._session.scalars(
            select(ApiConsumer).order_by(ApiConsumer.created_at, ApiConsumer.id)
        )
        return list(result)

    async def get(self, consumer_id: str) -> ApiConsumer | None:
        return await self._session.get(ApiConsumer, consumer_id)

    async def get_by_name(self, name: str) -> ApiConsumer | None:
        return await self._session.scalar(select(ApiConsumer).where(ApiConsumer.name == name))

    async def create(
        self,
        *,
        name: str,
        consumer_type: str,
        role: str,
        key_hash: str,
        scopes: Sequence[ScopeRef] = (),
        expires_at: datetime | None = None,
        created_by: str | None = None,
    ) -> ApiConsumer:
        consumer = ApiConsumer(
            name=name,
            consumer_type=consumer_type,
            role=role,
            key_hash=key_hash,
            enabled=True,
            expires_at=expires_at,
            created_by=created_by,
            updated_by=created_by,
            scopes=[
                ConsumerScope(project_id=scope.project_id, app_id=scope.app_id) for scope in scopes
            ],
        )
        self._session.add(consumer)
        await self._session.commit()
        await self._session.refresh(consumer)
        return consumer

    async def update(
        self,
        consumer_id: str,
        *,
        name: str | None = None,
        role: str | None = None,
        enabled: bool | None = None,
        scopes: Sequence[ScopeRef] | None = None,
        expires_at: datetime | None = None,
        revoked_at: datetime | None = None,
        updated_by: str | None = None,
    ) -> ApiConsumer | None:
        """Partial update; `None` means "leave unchanged" for every field."""
        consumer = await self.get(consumer_id)
        if consumer is None:
            return None
        if name is not None:
            consumer.name = name
        if role is not None:
            consumer.role = role
        if enabled is not None:
            consumer.enabled = enabled
        if scopes is not None:
            consumer.scopes = [
                ConsumerScope(project_id=scope.project_id, app_id=scope.app_id) for scope in scopes
            ]
        if expires_at is not None:
            consumer.expires_at = expires_at
        if revoked_at is not None:
            consumer.revoked_at = revoked_at
        if updated_by is not None:
            consumer.updated_by = updated_by
        await self._session.commit()
        await self._session.refresh(consumer)
        return consumer

    async def replace_key_hash(
        self, consumer_id: str, key_hash: str, *, rotated_by: str | None = None
    ) -> ApiConsumer | None:
        """Rotate: overwrite the stored hash (the old key stops working immediately)."""
        from datetime import UTC

        consumer = await self.get(consumer_id)
        if consumer is None:
            return None
        consumer.key_hash = key_hash
        consumer.rotated_at = datetime.now(UTC)
        consumer.rotation_count = int(consumer.rotation_count or 0) + 1
        if rotated_by is not None:
            consumer.updated_by = rotated_by
        await self._session.commit()
        await self._session.refresh(consumer)
        return consumer

    async def delete(self, consumer_id: str) -> bool:
        consumer = await self.get(consumer_id)
        if consumer is None:
            return False
        await self._session.delete(consumer)
        await self._session.commit()
        return True
