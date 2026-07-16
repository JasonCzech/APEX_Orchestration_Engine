"""Append-only security audit logging.

Audit writes are best-effort at request time: authorization must never become
unavailable because the audit database is temporarily unreachable. The table is
still durable and hash-chained when writes succeed.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from inspect import getattr_static
from typing import Any, TypeVar, cast

import structlog
from sqlalchemy import delete, desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apex.auth.identity import ConsumerIdentity
from apex.domain.diagnostics import bounded_diagnostic, safe_type_name
from apex.domain.durable_evidence import sanitize_durable_object, sanitize_durable_text
from apex.persistence.audit_lock import AUDIT_CHAIN_LOCK_KEY
from apex.persistence.models import AuditLog

logger = structlog.get_logger(__name__)

_AUDIT_TEXT_LIMITS = {
    "category": 64,
    "action": 128,
    "decision": 32,
    "principal_id": 255,
    "principal_type": 64,
    "principal_role": 32,
    "request_method": 16,
    "request_path": 2048,
    "request_id": 255,
    "ip_address": 255,
    "user_agent": 1024,
    "resource_type": 128,
    "resource_id": 255,
}
_AUDIT_REASON_LIMIT = 4096
AUDIT_EXPORT_PAGE_SIZE = 250
_UNMATCHED_ROUTE = "<unmatched-route>"
_AUDIT_HEADER_SCAN_LIMIT = 256
_AUDIT_HEADERS = {
    b"x-request-id": _AUDIT_TEXT_LIMITS["request_id"],
    b"user-agent": _AUDIT_TEXT_LIMITS["user_agent"],
}

_PageValue = TypeVar("_PageValue")


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


class AuditReadConsistencyError(RuntimeError):
    """A paged audit read changed before its captured watermark was consumed."""


@dataclass(frozen=True)
class _AuditRetentionPrefix:
    """The maximal chain-order prefix whose events precede a cutoff."""

    count: int
    anchor_id: str | None
    anchor_chain_seq: int | None
    first_retained_chain_seq: int | None


def validate_retention_cutoff(before: datetime, *, now: datetime | None = None) -> None:
    """Reject ambiguous or future retention windows before destructive work."""

    if before.tzinfo is None or before.utcoffset() is None:
        raise ValueError("before must include a timezone offset")
    current = now or datetime.now(UTC)
    if before > current:
        raise ValueError("before must not be in the future")


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
        chain_seq = await self._next_chain_seq(head)
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

    async def _next_chain_seq(self, head: AuditLog | None) -> int:
        # PostgreSQL sequences are not transactional: a rollback after nextval()
        # leaves a permanent gap that gapless chain verification reports as
        # tampering. The advisory xact lock above already serializes writers, so
        # allocating from the locked head is both transactional and race-free.
        return (head.chain_seq + 1) if head is not None else 1

    async def _chain_head(self) -> AuditLog | None:
        stmt = (
            select(AuditLog)
            .order_by(desc(AuditLog.chain_seq).nullslast(), desc(AuditLog.at))
            .limit(1)
        )
        return await self._session.scalar(stmt)

    async def _lock_chain(self) -> None:
        if _dialect_name(self._session) != "postgresql":
            return
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)").bindparams(
                lock_key=AUDIT_CHAIN_LOCK_KEY
            )
        )

    async def verify_chain(self, *, allow_truncated: bool = False) -> AuditChainVerification:
        previous_hash: str | None = None
        expected_chain_seq = 1
        checked = 0
        first = True
        async for row in self._iter_rows():
            if first and allow_truncated:
                previous_hash = row.previous_hash
                expected_chain_seq = row.chain_seq
            first = False
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
        return "\n".join([line async for line in self.iter_jsonl()])

    async def export_cef(self) -> str:
        return "\n".join([line async for line in self.iter_cef()])

    async def iter_jsonl(self) -> AsyncIterator[str]:
        async for line in self._iter_materialized_values(
            lambda row: json.dumps(_row_dict(row), sort_keys=True)
        ):
            yield line

    async def iter_cef(self) -> AsyncIterator[str]:
        async for line in self._iter_materialized_values(_row_cef):
            yield line

    async def _iter_rows(self) -> AsyncIterator[AuditLog]:
        async for row in self._iter_materialized_values(lambda row: row):
            yield row

    async def _iter_materialized_values(
        self,
        materialize: Callable[[AuditLog], _PageValue],
    ) -> AsyncIterator[_PageValue]:
        """Yield a finite, watermark-bounded export without pinning a DB connection.

        Production AsyncSession objects expose ``stream_scalars``. Small unit
        fakes without that API retain the original in-memory path; real reads
        use keyset pagination and release their transaction before any value is
        handed to a potentially slow caller.
        """

        if getattr(self._session, "stream_scalars", None) is None:
            for row in await self._rows():
                yield materialize(row)
            return

        bounds = await self._session.execute(
            select(func.min(AuditLog.chain_seq), func.max(AuditLog.chain_seq))
        )
        first_chain_seq_value, watermark_value = bounds.one()
        await self._release_export_read_transaction()
        if watermark_value is None:
            return
        if first_chain_seq_value is None:
            raise AuditReadConsistencyError("audit export captured inconsistent chain bounds")
        first_chain_seq = int(first_chain_seq_value)
        watermark = int(watermark_value)
        last_chain_seq: int | None = None
        expected_chain_seq = first_chain_seq

        while True:
            statement = select(AuditLog).where(AuditLog.chain_seq <= watermark)
            if last_chain_seq is not None:
                statement = statement.where(AuditLog.chain_seq > last_chain_seq)
            statement = statement.order_by(AuditLog.chain_seq).limit(AUDIT_EXPORT_PAGE_SIZE)

            try:
                rows = list(await self._session.scalars(statement))
                if rows:
                    for row in rows:
                        actual_chain_seq = int(row.chain_seq)
                        if actual_chain_seq != expected_chain_seq:
                            raise AuditReadConsistencyError(
                                "audit export chain changed before watermark "
                                f"{watermark}: expected sequence {expected_chain_seq}, "
                                f"found {actual_chain_seq}"
                            )
                        expected_chain_seq += 1
                    next_chain_seq = int(rows[-1].chain_seq)
                    if last_chain_seq is not None and next_chain_seq <= last_chain_seq:
                        raise RuntimeError("audit export keyset did not advance")
                    values = [materialize(row) for row in rows]
                    # Detaching prevents a custom/default expire-on-commit
                    # session from lazily checking out another connection while
                    # the already-materialized page is being consumed.
                    expunge = getattr(self._session, "expunge", None)
                    if expunge is not None:
                        for row in rows:
                            expunge(row)
                else:
                    next_chain_seq = last_chain_seq
                    values = []
                if not rows and expected_chain_seq <= watermark:
                    raise AuditReadConsistencyError(
                        "audit export chain ended before captured watermark "
                        f"{watermark}; next expected sequence is {expected_chain_seq}"
                    )
                if rows and len(rows) < AUDIT_EXPORT_PAGE_SIZE and next_chain_seq != watermark:
                    raise AuditReadConsistencyError(
                        "audit export page ended before captured watermark "
                        f"{watermark}; last sequence is {next_chain_seq}"
                    )
                await self._release_export_read_transaction()
            except BaseException:
                await self._rollback_export_read_transaction()
                raise

            for value in values:
                yield value

            if not rows or next_chain_seq == watermark:
                return
            assert next_chain_seq is not None
            last_chain_seq = next_chain_seq

    async def _release_export_read_transaction(self) -> None:
        """Release a page read without making any caller-owned writes durable."""

        if any(
            bool(getattr(self._session, attribute, ())) for attribute in ("new", "dirty", "deleted")
        ):
            raise RuntimeError("audit export session contains pending mutations")
        # ``new``/``dirty``/``deleted`` cannot see Core or bulk DML issued through
        # ``execute()``.  A commit here could therefore publish unrelated writes
        # merely because a caller streamed an audit export through the same
        # request-scoped session.  Every value yielded by the exporter has already
        # been materialized (and ORM rows are detached), so rolling back the read
        # snapshot is both sufficient and the only safe release boundary.
        await self._session.rollback()

    async def _rollback_export_read_transaction(self) -> None:
        rollback = getattr(self._session, "rollback", None)
        if rollback is None:
            return
        try:
            await rollback()
        except BaseException:
            # Preserve the query/serialization/cancellation exception. Request
            # dependency cleanup remains the final safety net for a dead pool.
            pass

    async def retention_summary(
        self, *, before: datetime, retain_anchor: bool = True
    ) -> AuditRetentionSummary:
        validate_retention_cutoff(before)
        if getattr(self._session, "stream_scalars", None) is None:
            rows = _contiguous_retention_prefix(await self._rows(), before)
            preserved_anchor_id = rows[-1].id if retain_anchor and rows else None
            candidates = max(len(rows) - 1, 0) if retain_anchor and rows else len(rows)
            return AuditRetentionSummary(
                before=before,
                candidates=candidates,
                preserved_anchor_id=preserved_anchor_id,
            )
        prefix = await self._retention_prefix(before)
        preserved_anchor_id = prefix.anchor_id if retain_anchor else None
        candidates = max(prefix.count - 1, 0) if retain_anchor else prefix.count
        return AuditRetentionSummary(
            before=before,
            candidates=candidates,
            preserved_anchor_id=preserved_anchor_id,
        )

    async def prune_before(self, *, before: datetime, retain_anchor: bool = True) -> int:
        validate_retention_cutoff(before)
        await self._lock_chain()
        if getattr(self._session, "stream_scalars", None) is None:
            rows = _contiguous_retention_prefix(await self._rows(), before)
            if retain_anchor and rows:
                rows = rows[:-1]
            ids = [row.id for row in rows]
            if not ids:
                await self._session.commit()
                return 0
            await self._session.execute(delete(AuditLog).where(AuditLog.id.in_(ids)))
            await self._session.commit()
            return len(ids)
        prefix = await self._retention_prefix(before)
        deleted = max(prefix.count - 1, 0) if retain_anchor else prefix.count
        if deleted == 0:
            # Release the transaction-scoped writer lock promptly even when the
            # selected window has nothing safe to remove.
            await self._session.commit()
            return 0
        if retain_anchor:
            assert prefix.anchor_chain_seq is not None
            predicate = AuditLog.chain_seq < prefix.anchor_chain_seq
        elif prefix.first_retained_chain_seq is None:
            assert prefix.anchor_chain_seq is not None
            predicate = AuditLog.chain_seq <= prefix.anchor_chain_seq
        else:
            predicate = AuditLog.chain_seq < prefix.first_retained_chain_seq
        await self._session.execute(delete(AuditLog).where(predicate))
        # One transaction keeps the advisory lock across boundary selection and
        # deletion; per-batch commits would let appenders race into the window.
        await self._session.commit()
        return deleted

    async def _retention_prefix(self, before: datetime) -> _AuditRetentionPrefix:
        """Select a prefix boundary by chain order, never by arbitrary old rows."""

        first_retained_chain_seq = await self._session.scalar(
            select(func.min(AuditLog.chain_seq)).where(AuditLog.at >= before)
        )
        prefix_filter = (
            AuditLog.chain_seq < first_retained_chain_seq
            if first_retained_chain_seq is not None
            else None
        )
        count_stmt = select(func.count()).select_from(AuditLog)
        anchor_stmt = select(AuditLog).order_by(AuditLog.chain_seq.desc()).limit(1)
        if prefix_filter is not None:
            count_stmt = count_stmt.where(prefix_filter)
            anchor_stmt = anchor_stmt.where(prefix_filter)
        count = int(await self._session.scalar(count_stmt) or 0)
        anchor = await self._session.scalar(anchor_stmt) if count else None
        return _AuditRetentionPrefix(
            count=count,
            anchor_id=anchor.id if anchor is not None else None,
            anchor_chain_seq=anchor.chain_seq if anchor is not None else None,
            first_retained_chain_seq=first_retained_chain_seq,
        )

    async def _rows(self) -> list[AuditLog]:
        result = await self._session.scalars(select(AuditLog).order_by(AuditLog.chain_seq))
        return list(result)


def _contiguous_retention_prefix(rows: list[AuditLog], before: datetime) -> list[AuditLog]:
    prefix: list[AuditLog] = []
    for row in rows:
        if row.at >= before:
            break
        prefix.append(row)
    return prefix


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
            error_type=safe_type_name(exc),
        )


def request_audit_event(
    scope: Mapping[str, Any],
    *,
    status_code: int,
    reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> AuditEvent:
    safe_scope: Mapping[str, Any] = scope if type(scope) is dict else {}
    headers = _headers(safe_scope)
    client = safe_scope.get("client")
    identity = _identity_from_scope(safe_scope)
    route_template = _safe_audit_route_template(safe_scope)
    operation_id = _safe_audit_operation_id(safe_scope)
    method = safe_scope.get("method")
    request_method = (
        method
        if type(method) is str
        and len(method) <= _AUDIT_TEXT_LIMITS["request_method"]
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in method)
        else ""
    )
    client_host = client[0] if type(client) is tuple and client else None
    ip_address = (
        client_host
        if type(client_host) is str
        and len(client_host) <= _AUDIT_TEXT_LIMITS["ip_address"]
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in client_host)
        else None
    )
    return AuditEvent(
        category="authz_decision",
        action=operation_id or route_template,
        decision=_decision_for_status(status_code),
        reason=reason,
        principal_id=identity.consumer_id if identity else None,
        principal_type=identity.consumer_type.value if identity else None,
        principal_role=identity.role.value if identity else None,
        principal_scopes={"scopes": [s.model_dump(mode="json") for s in identity.scopes]}
        if identity
        else {},
        request_method=request_method,
        request_path=route_template,
        request_id=headers.get("x-request-id"),
        ip_address=ip_address,
        user_agent=headers.get("user-agent"),
        status_code=status_code,
        extra=extra if type(extra) is dict else {},
    )


def _safe_audit_route_template(scope: Mapping[str, Any]) -> str:
    """Return only server-owned routing metadata for durable request attribution."""

    route = scope.get("route")
    for attribute in ("path", "path_format"):
        try:
            value = getattr_static(route, attribute, None)
        except Exception:
            value = None
        if (
            type(value) is str
            and value.startswith("/")
            and len(value) <= _AUDIT_TEXT_LIMITS["request_path"]
            and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        ):
            return value
    # Middleware-generated rejections can occur before either FastAPI or the
    # native LangGraph router has matched the request.  The concrete scope path
    # is caller-controlled, so an opaque marker is the only safe fallback.
    return _UNMATCHED_ROUTE


def _safe_audit_operation_id(scope: Mapping[str, Any]) -> str | None:
    try:
        value = getattr_static(scope.get("route"), "operation_id", None)
    except Exception:
        value = None
    if (
        type(value) is str
        and value
        and len(value) <= _AUDIT_TEXT_LIMITS["action"]
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        return value
    return None


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
    bounded = {
        name: sanitize_durable_text(getattr(event, name), limit)
        for name, limit in _AUDIT_TEXT_LIMITS.items()
    }
    return replace(
        event,
        **bounded,
        reason=sanitize_durable_text(event.reason, _AUDIT_REASON_LIMIT),
        principal_scopes=sanitize_durable_object(event.principal_scopes),
        extra=sanitize_durable_object(event.extra),
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
        f"{_cef_header_escape(row.category)}|{_cef_header_escape(row.action)}|"
        f"{_cef_severity(row)}|"
        f"{extension}"
    )


def _cef_escape(value: Any) -> str:
    return _cef_escape_scalar(value, delimiter="=")


def _cef_header_escape(value: Any) -> str:
    return _cef_escape_scalar(value, delimiter="|")


def _cef_escape_scalar(value: Any, *, delimiter: str) -> str:
    """Render one bounded CEF scalar without hooks or framing controls."""

    rendered = bounded_diagnostic(value, max_chars=4_096)
    escaped: list[str] = []
    for character in rendered:
        codepoint = ord(character)
        if character == "\\":
            escaped.append("\\\\")
        elif character == delimiter:
            escaped.append(f"\\{delimiter}")
        elif character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif codepoint < 0x20 or codepoint == 0x7F or 0x80 <= codepoint <= 0x9F:
            escaped.append("?")
        else:
            escaped.append(character)
    return "".join(escaped)


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
    if type(scope) is not dict:
        return None
    state = scope.get("state")
    identity: Any = None
    if type(state) is dict:
        identity = state.get("identity")
    else:
        try:
            identity = getattr_static(state, "identity", None)
        except Exception:
            identity = None
    return identity if isinstance(identity, ConsumerIdentity) else None


def _dialect_name(session: AsyncSession) -> str | None:
    bind = getattr(session, "bind", None)
    dialect = getattr(bind, "dialect", None)
    name = getattr(dialect, "name", None)
    return str(name) if name is not None else None


def _headers(scope: Mapping[str, Any]) -> dict[str, str]:
    """Extract only bounded audit headers without coercing caller-owned objects."""

    out: dict[str, str] = {}
    if type(scope) is not dict:
        return out
    headers = scope.get("headers")
    if type(headers) not in {list, tuple}:
        return out
    bounded_headers = cast("list[Any] | tuple[Any, ...]", headers)
    seen: set[bytes] = set()
    for index, item in enumerate(bounded_headers):
        if index >= _AUDIT_HEADER_SCAN_LIMIT:
            break
        if type(item) is not tuple or len(item) != 2:
            continue
        raw_key, raw_value = item
        if type(raw_key) is not bytes or len(raw_key) > 64:
            continue
        key = raw_key.lower()
        limit = _AUDIT_HEADERS.get(key)
        if limit is None:
            continue
        # Duplicate security-attribution fields are ambiguous. Omit them rather
        # than allowing ordering differences between proxies to rewrite an event.
        if key in seen:
            out.pop(key.decode("ascii"), None)
            continue
        seen.add(key)
        if type(raw_value) is not bytes or len(raw_value) > limit:
            continue
        out[key.decode("ascii")] = raw_value.decode("latin-1")
    return out
