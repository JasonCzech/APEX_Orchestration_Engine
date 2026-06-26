"""Append-only security audit logging.

Audit writes are best-effort at request time: authorization must never become
unavailable because the audit database is temporarily unreachable. The table is
still durable and hash-chained when writes succeed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apex.auth.identity import ConsumerIdentity
from apex.persistence.models import AuditLog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AuditEvent:
    category: str
    action: str
    decision: str
    reason: str | None = None
    principal_id: str | None = None
    principal_type: str | None = None
    principal_role: str | None = None
    principal_scopes: dict[str, Any] = field(default_factory=dict)
    request_method: str | None = None
    request_path: str | None = None
    request_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    status_code: int | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def event_from_identity(
    *,
    identity: ConsumerIdentity | None,
    category: str,
    action: str,
    decision: str,
    reason: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> AuditEvent:
    return AuditEvent(
        category=category,
        action=action,
        decision=decision,
        reason=reason,
        principal_id=identity.consumer_id if identity else None,
        principal_type=identity.consumer_type.value if identity else None,
        principal_role=identity.role.value if identity else None,
        principal_scopes={"scopes": [s.model_dump(mode="json") for s in identity.scopes]}
        if identity
        else {},
        resource_type=resource_type,
        resource_id=resource_id,
        extra=extra or {},
    )


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: AuditEvent) -> AuditLog:
        previous_hash = await self._previous_hash()
        event_hash = _event_hash(event, previous_hash)
        row = AuditLog(
            category=event.category,
            action=event.action,
            decision=event.decision,
            reason=event.reason,
            principal_id=event.principal_id,
            principal_type=event.principal_type,
            principal_role=event.principal_role,
            principal_scopes=event.principal_scopes,
            request_method=event.request_method,
            request_path=event.request_path,
            request_id=event.request_id,
            ip_address=event.ip_address,
            user_agent=event.user_agent,
            status_code=event.status_code,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            extra=event.extra,
            previous_hash=previous_hash,
            event_hash=event_hash,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def _previous_hash(self) -> str | None:
        stmt = select(AuditLog.event_hash).order_by(desc(AuditLog.at), desc(AuditLog.id)).limit(1)
        return await self._session.scalar(stmt)


async def append_audit_event(
    session_factory: async_sessionmaker[AsyncSession], event: AuditEvent
) -> None:
    async with session_factory() as session:
        await AuditService(session).append(event)


async def append_audit_event_best_effort(event: AuditEvent) -> None:
    try:
        from apex.persistence.db import get_sessionmaker

        await append_audit_event(get_sessionmaker(), event)
    except Exception as exc:  # noqa: BLE001 - audit must not break the request path
        logger.warning(
            "apex.audit.write_failed",
            category=event.category,
            action=event.action,
            decision=event.decision,
            error=f"{exc.__class__.__name__}: {exc}",
        )


def request_audit_event(
    scope: Mapping[str, Any],
    *,
    status_code: int,
    reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> AuditEvent:
    headers = _headers(scope)
    client = scope.get("client")
    return AuditEvent(
        category="authz_decision",
        action=str(getattr(scope.get("route"), "operation_id", None) or scope.get("path") or ""),
        decision=_decision_for_status(status_code),
        reason=reason,
        request_method=str(scope.get("method") or ""),
        request_path=str(scope.get("path") or ""),
        request_id=headers.get("x-request-id"),
        ip_address=str(client[0]) if isinstance(client, tuple) and client else None,
        user_agent=headers.get("user-agent"),
        status_code=status_code,
        extra=extra or {},
    )


def _event_hash(event: AuditEvent, previous_hash: str | None) -> str:
    payload = {
        "previous_hash": previous_hash,
        "event": {
            key: _jsonable(value)
            for key, value in event.__dict__.items()
            if key not in {"previous_hash", "event_hash"}
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return value


def _decision_for_status(status_code: int) -> str:
    return {
        401: "unauthenticated",
        403: "denied",
        429: "rate_limited",
    }.get(status_code, "observed")


def _headers(scope: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_key, raw_value in scope.get("headers") or []:
        try:
            key = raw_key.decode("latin-1").lower()
            value = raw_value.decode("latin-1")
        except AttributeError:
            key = str(raw_key).lower()
            value = str(raw_value)
        out[key] = value
    return out
