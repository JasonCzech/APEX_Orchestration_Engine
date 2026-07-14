"""Async repository for the ApiConsumer aggregate (consumer + scopes).

Raw API keys are never persisted: callers hash with `apex.auth.service.hash_api_key`
before handing the digest to this repository.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apex.auth.identity import ScopeRef
from apex.persistence.models import ApiConsumer, ConsumerDeletionRecord, ConsumerKey, ConsumerScope


class DuplicateConsumerNameError(Exception):
    """The database rejected a duplicate API-consumer name."""


class ConsumersRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_all(self) -> list[ApiConsumer]:
        result = await self._session.scalars(
            select(ApiConsumer)
            .where(ApiConsumer.deleted_at.is_(None))
            .order_by(ApiConsumer.created_at, ApiConsumer.id)
        )
        return list(result)

    async def get(self, consumer_id: str) -> ApiConsumer | None:
        consumer = await self._session.get(ApiConsumer, consumer_id)
        if consumer is None or consumer.deleted_at is not None:
            return None
        return consumer

    async def get_for_update(self, consumer_id: str) -> ApiConsumer | None:
        result = await self._session.scalars(
            select(ApiConsumer)
            .where(ApiConsumer.id == consumer_id)
            .options(selectinload(ApiConsumer.scopes), selectinload(ApiConsumer.keys))
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        consumer = result.first()
        if consumer is None or consumer.deleted_at is not None:
            return None
        return consumer

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
            keys=[
                ConsumerKey(
                    key_hash=key_hash,
                    expires_at=expires_at,
                    created_by=created_by,
                )
            ],
        )
        self._session.add(consumer)
        await self._commit_name_write(consumer)
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
        consumer = await self.get_for_update(consumer_id)
        if consumer is None:
            return None
        return await self.update_existing(
            consumer,
            name=name,
            role=role,
            enabled=enabled,
            scopes=scopes,
            expires_at=expires_at,
            revoked_at=revoked_at,
            updated_by=updated_by,
        )

    async def update_existing(
        self,
        consumer: ApiConsumer,
        *,
        name: str | None = None,
        role: str | None = None,
        enabled: bool | None = None,
        scopes: Sequence[ScopeRef] | None = None,
        expires_at: datetime | None = None,
        revoked_at: datetime | None = None,
        updated_by: str | None = None,
    ) -> ApiConsumer:
        """Partial update of an already-loaded consumer row."""
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
        await self._commit_name_write(consumer)
        return consumer

    async def _commit_name_write(self, consumer: ApiConsumer) -> None:
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if _is_duplicate_consumer_name(exc):
                raise DuplicateConsumerNameError(str(exc.orig)) from exc
            raise
        await self._session.refresh(consumer)

    async def replace_key_hash(
        self,
        consumer_id: str,
        key_hash: str,
        *,
        rotated_by: str | None = None,
        grace_expires_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> ApiConsumer | None:
        """Rotate: issue a new key; old active keys may survive until grace_expires_at."""
        consumer = await self.get_for_update(consumer_id)
        if consumer is None:
            return None
        now = datetime.now(UTC)
        active_keys = [
            key
            for key in consumer.keys
            if key.revoked_at is None and (key.expires_at is None or key.expires_at > now)
        ]
        for key in active_keys:
            if grace_expires_at is None or grace_expires_at <= now:
                key.revoked_at = now
            elif key.expires_at is None or key.expires_at > grace_expires_at:
                key.expires_at = grace_expires_at
        rotated_from_id = active_keys[0].id if active_keys else None
        consumer.keys.append(
            ConsumerKey(
                key_hash=key_hash,
                expires_at=expires_at or consumer.expires_at,
                rotated_from_id=rotated_from_id,
                created_by=rotated_by,
            )
        )
        consumer.key_hash = key_hash
        consumer.rotated_at = now
        consumer.rotation_count = int(consumer.rotation_count or 0) + 1
        if rotated_by is not None:
            consumer.updated_by = rotated_by
        await self._session.commit()
        await self._session.refresh(consumer)
        return consumer

    async def delete(self, consumer_id: str, *, deleted_by: str | None = None) -> bool:
        consumer = await self.get_for_update(consumer_id)
        if consumer is None:
            return False
        return await self.delete_existing(consumer, deleted_by=deleted_by)

    async def delete_existing(
        self, consumer: ApiConsumer, *, deleted_by: str | None = None
    ) -> bool:
        """Soft-delete an already locked aggregate and its active keys."""

        if consumer.deleted_at is not None:
            return False
        deleted_at = datetime.now(UTC)
        consumer.deleted_at = deleted_at
        consumer.revoked_at = consumer.revoked_at or deleted_at
        consumer.enabled = False
        consumer.updated_by = deleted_by
        for key in consumer.keys:
            key.revoked_at = key.revoked_at or deleted_at
        self._session.add(
            ConsumerDeletionRecord(
                consumer_id=consumer.id,
                deleted_at=deleted_at,
                deleted_by=deleted_by,
                name=consumer.name,
                consumer_type=consumer.consumer_type,
                role=consumer.role,
                scopes={
                    "scopes": [
                        {"project_id": scope.project_id, "app_id": scope.app_id}
                        for scope in consumer.scopes
                    ]
                },
            )
        )
        await self._session.commit()
        return True


def _is_duplicate_consumer_name(exc: IntegrityError) -> bool:
    constraint_name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    message = str(exc.orig).lower()
    return constraint_name == "uq_api_consumers_name" or (
        "uq_api_consumers_name" in message
        or ("unique constraint failed" in message and "api_consumers.name" in message)
    )
