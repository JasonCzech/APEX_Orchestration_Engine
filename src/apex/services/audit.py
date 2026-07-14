"""Append-only security audit logging.

Audit writes are best-effort at request time: authorization must never become
unavailable because the audit database is temporarily unreachable. The table is
still durable and hash-chained when writes succeed.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import delete, desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apex.auth.identity import ConsumerIdentity
from apex.persistence.models import AuditLog

logger = structlog.get_logger(__name__)
_CHAIN_LOCK_KEY = 0x4150455841554449  # "APEXAUDI" as a signed 64-bit advisory-lock key.


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
    at: datetime | None = None
    event_nonce: str | None = None


@dataclass(frozen=True)
class AuditChainVerification:
    ok: bool
    checked: int
    first_error: str | None = None
    last_hash: str | None = None


@dataclass(frozen=True)
class AuditRetentionSummary:
    before: datetime
    candidates: int
    preserved_anchor_id: str | None = None


def event_from_identity(
    *,
    identity: ConsumerIdentity | None,
    category: str,
    action: str,
    decision: str,
    reason: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    status_code: int | None = None,
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
        status_code=status_code,
        resource_type=resource_type,
        resource_id=resource_id,
        extra=extra or {},
    )


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: AuditEvent) -> AuditLog:
        await self._lock_chain()
        event = _materialize_event(event)
        head = await self._chain_head()
        chain_seq = (head.chain_seq + 1) if head is not None and head.chain_seq is not None else 1
        previous_hash = head.event_hash if head is not None else None
        event_hash = _event_hash(event, previous_hash)
        row = AuditLog(
            chain_seq=chain_seq,
            at=event.at,
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
            event_nonce=event.event_nonce,
            previous_hash=previous_hash,
            event_hash=event_hash,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def _chain_head(self) -> AuditLog | None:
        stmt = select(AuditLog).order_by(desc(AuditLog.chain_seq).nullslast(), desc(AuditLog.at)).limit(1)
        return await self._session.scalar(stmt)

    async def _lock_chain(self) -> None:
        if _dialect_name(self._session) != "postgresql":
            return
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)").bindparams(lock_key=_CHAIN_LOCK_KEY)
        )

    async def verify_chain(self, *, allow_truncated: bool = False) -> AuditChainVerification:
        rows = await self._rows()
        previous_hash: str | None = None
        expected_chain_seq = 1
        if allow_truncated and rows:
            previous_hash = rows[0].previous_hash
            expected_chain_seq = rows[0].chain_seq
        checked = 0
        for row in rows:
            checked += 1
            if row.chain_seq != expected_chain_seq:
                return AuditChainVerification(
                    ok=False,
                    checked=checked,
                    first_error=(
                        f"row {row.id} chain_seq mismatch: expected "
                        f"{expected_chain_seq}, found {row.chain_seq}"
                    ),
                    last_hash=previous_hash,
                )
            if row.previous_hash != previous_hash:
                return AuditChainVerification(
                    ok=False,
                    checked=checked,
                    first_error=(
                        f"row {row.id} previous_hash mismatch: expected "
                        f"{previous_hash!r}, found {row.previous_hash!r}"
                    ),
                    last_hash=previous_hash,
                )
            row_event = _event_from_row(row)
            # Rows written before migration 0010 do not have an event nonce and
            # were hashed without the post-migration timestamp/nonce fields.
            expected_hash = (
                _legacy_event_hash(row_event, previous_hash)
                if row.event_nonce is None
                else _event_hash(row_event, previous_hash)
            )
            if row.event_hash != expected_hash:
                return AuditChainVerification(
                    ok=False,
                    checked=checked,
                    first_error=f"row {row.id} event_hash mismatch",
                    last_hash=previous_hash,
                )
            previous_hash = row.event_hash
            expected_chain_seq += 1
        return AuditChainVerification(ok=True, checked=checked, last_hash=previous_hash)

    async def export_jsonl(self) -> str:
        return "\n".join(json.dumps(_row_dict(row), sort_keys=True) for row in await self._rows())

    async def export_cef(self) -> str:
        return "\n".join(_row_cef(row) for row in await self._rows())

    async def retention_summary(
        self, *, before: datetime, retain_anchor: bool = True
    ) -> AuditRetentionSummary:
        rows = [row for row in await self._rows() if row.at < before]
        preserved_anchor_id = rows[-1].id if retain_anchor and rows else None
        candidates = max(len(rows) - 1, 0) if retain_anchor and rows else len(rows)
        return AuditRetentionSummary(
            before=before,
            candidates=candidates,
            preserved_anchor_id=preserved_anchor_id,
        )

    async def prune_before(self, *, before: datetime, retain_anchor: bool = True) -> int:
        rows = [row for row in await self._rows() if row.at < before]
        if retain_anchor and rows:
            rows = rows[:-1]
        ids = [row.id for row in rows]
        if not ids:
            return 0
        await self._session.execute(delete(AuditLog).where(AuditLog.id.in_(ids)))
        await self._session.commit()
        return len(ids)

    async def _rows(self) -> list[AuditLog]:
        result = await self._session.scalars(select(AuditLog).order_by(AuditLog.chain_seq))
        return list(result)


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
    identity = _identity_from_scope(scope)
    return AuditEvent(
        category="authz_decision",
        action=str(getattr(scope.get("route"), "operation_id", None) or scope.get("path") or ""),
        decision=_decision_for_status(status_code),
        reason=reason,
        principal_id=identity.consumer_id if identity else None,
        principal_type=identity.consumer_type.value if identity else None,
        principal_role=identity.role.value if identity else None,
        principal_scopes={"scopes": [s.model_dump(mode="json") for s in identity.scopes]}
        if identity
        else {},
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


def _legacy_event_hash(event: AuditEvent, previous_hash: str | None) -> str:
    payload = {
        "previous_hash": previous_hash,
        "event": {
            key: _jsonable(value)
            for key, value in event.__dict__.items()
            if key not in {"previous_hash", "event_hash", "at", "event_nonce"}
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _materialize_event(event: AuditEvent) -> AuditEvent:
    return replace(
        event,
        at=event.at or datetime.now(UTC),
        event_nonce=secrets.token_hex(8),
    )


def _event_from_row(row: AuditLog) -> AuditEvent:
    return AuditEvent(
        category=row.category,
        action=row.action,
        decision=row.decision,
        reason=row.reason,
        principal_id=row.principal_id,
        principal_type=row.principal_type,
        principal_role=row.principal_role,
        principal_scopes=dict(row.principal_scopes or {}),
        request_method=row.request_method,
        request_path=row.request_path,
        request_id=row.request_id,
        ip_address=row.ip_address,
        user_agent=row.user_agent,
        status_code=row.status_code,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        extra=dict(row.extra or {}),
        at=row.at,
        event_nonce=row.event_nonce,
    )


def _row_dict(row: AuditLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "chain_seq": row.chain_seq,
        "at": _jsonable(row.at),
        "category": row.category,
        "action": row.action,
        "decision": row.decision,
        "reason": row.reason,
        "principal_id": row.principal_id,
        "principal_type": row.principal_type,
        "principal_role": row.principal_role,
        "principal_scopes": row.principal_scopes or {},
        "request_method": row.request_method,
        "request_path": row.request_path,
        "request_id": row.request_id,
        "ip_address": row.ip_address,
        "user_agent": row.user_agent,
        "status_code": row.status_code,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "extra": row.extra or {},
        "event_nonce": row.event_nonce,
        "previous_hash": row.previous_hash,
        "event_hash": row.event_hash,
    }


def _row_cef(row: AuditLog) -> str:
    fields = {
        "rt": _jsonable(row.at),
        "cn1Label": "chain_seq",
        "cn1": row.chain_seq,
        "suser": row.principal_id,
        "spriv": row.principal_role,
        "requestMethod": row.request_method,
        "request": row.request_path,
        "src": row.ip_address,
        "outcome": row.decision,
        "msg": row.reason,
        "cs1Label": "resource_type",
        "cs1": row.resource_type,
        "cs2Label": "resource_id",
        "cs2": row.resource_id,
        "cs3Label": "event_hash",
        "cs3": row.event_hash,
    }
    extension = " ".join(
        f"{key}={_cef_escape(value)}" for key, value in fields.items() if value is not None
    )
    return (
        "CEF:0|APEX|Orchestration Engine|1|"
        f"{_cef_escape(row.category)}|{_cef_escape(row.action)}|{_cef_severity(row)}|"
        f"{extension}"
    )


def _cef_escape(value: Any) -> str:
    return (
        str(value).replace("\\", "\\\\").replace("=", "\\=").replace("\n", "\\n").replace("\r", "")
    )


def _cef_severity(row: AuditLog) -> int:
    if row.decision in {"denied", "unauthenticated"}:
        return 8
    if row.decision == "rate_limited":
        return 6
    if row.decision == "allowed":
        return 3
    status = row.status_code or 0
    if status >= 500:
        return 7
    if status >= 400:
        return 5
    return 2


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


def _identity_from_scope(scope: Mapping[str, Any]) -> ConsumerIdentity | None:
    state = scope.get("state")
    identity: Any = None
    if isinstance(state, Mapping):
        identity = state.get("identity")
    else:
        identity = getattr(state, "identity", None)
    return identity if isinstance(identity, ConsumerIdentity) else None


def _dialect_name(session: AsyncSession) -> str | None:
    bind = getattr(session, "bind", None)
    dialect = getattr(bind, "dialect", None)
    name = getattr(dialect, "name", None)
    return str(name) if name is not None else None


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
