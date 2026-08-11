"""Durable APEX Load native-control mirror and reporting-launch ledger."""

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apex.domain.diagnostics import contains_credential_material
from apex.persistence.models import (
    ApexLoadNativeReconcileLease,
    ApexLoadNativeRun,
    ApexLoadNativeScenario,
    ApexLoadNativeSchedule,
    Connection,
)

NATIVE_RUN_LAUNCH_LEASE = timedelta(minutes=12)
NATIVE_CONNECTION_RECONCILE_LEASE = timedelta(minutes=2)
NATIVE_CONNECTION_POLL_DELAY = timedelta(seconds=30)
_SAFE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}\Z")
_TERMINAL_STATUSES = frozenset({"COMPLETE", "FAILED", "ABORTED"})
_MAX_SNAPSHOT_TEXT = 2_048
_FINGERPRINT = re.compile(r"\A[0-9a-f]{64}\Z")


class NativeResourceIdempotencyConflictError(ValueError):
    """A native create key was reused for a different request or resource."""


@dataclass(frozen=True)
class NativeConnectionClaim:
    connection_id: str
    connection_version: datetime
    project_id: str
    claim_token: str


@dataclass(frozen=True)
class NativeRunLaunchClaim:
    id: str
    connection_id: str
    connection_version: datetime
    project_id: str
    remote_project_id: str
    remote_id: str
    status: str
    summary: dict[str, Any]
    idempotency_key: str
    claim_token: str


def _safe_identifier(value: Any, *, label: str, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if (
        type(value) is not str
        or _SAFE_ID.fullmatch(value) is None
        or contains_credential_material(value)
    ):
        raise ValueError(f"APEX Load {label} is invalid")
    return value


def _safe_idempotency_key(value: Any) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 255
        or any(not 0x21 <= ord(character) <= 0x7E for character in value)
    ):
        raise ValueError("APEX Load native create idempotency key is invalid")
    return value


def _safe_text(value: Any, *, maximum: int = _MAX_SNAPSHOT_TEXT) -> str | None:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or "\x00" in value
        or contains_credential_material(value)
    ):
        return None
    return value


def _safe_revision(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 1_000_000_000:
        return None
    return value


def _safe_timestamp(value: Any) -> datetime | None:
    if type(value) is not str or len(value) > 128:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _snapshot(value: dict[str, Any]) -> dict[str, Any]:
    """Persist only bounded scalar metadata, never arbitrary provider payloads."""

    snapshot: dict[str, Any] = {}
    for field in (
        "id",
        "name",
        "status",
        "project_id",
        "scenario_id",
        "schedule_id",
        "revision",
        "created_at",
        "started_at",
        "ended_at",
        "updated_at",
    ):
        item = value.get(field)
        if (
            item is None
            or type(item) in {bool, int}
            or (type(item) is float and math.isfinite(item))
        ):
            snapshot[field] = item
        else:
            safe = _safe_text(item)
            if safe is not None:
                snapshot[field] = safe
    return snapshot


def native_run_idempotency_key(
    connection_id: str,
    connection_version: datetime,
    remote_project_id: str,
    remote_id: str,
) -> str:
    """Stable fixed-size key for one provider-run connection generation."""

    _safe_identifier(connection_id, label="connection id")
    if type(connection_version) is not datetime:
        raise ValueError("APEX Load connection version is invalid")
    _safe_identifier(remote_project_id, label="remote project id")
    _safe_identifier(remote_id, label="run id")
    digest = hashlib.sha256(
        f"{connection_id}:{connection_version.isoformat()}:{remote_project_id}:{remote_id}".encode()
    ).hexdigest()
    return f"apex-load-native:{digest}"


class ApexLoadNativeRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def claim_enabled_connections(
        self,
        *,
        limit: int = 100,
        exclude_connection_ids: frozenset[str] = frozenset(),
    ) -> list[NativeConnectionClaim]:
        if not 1 <= limit <= 500:
            raise ValueError("APEX Load connection claim limit is invalid")
        if not isinstance(exclude_connection_ids, frozenset) or len(exclude_connection_ids) > 500:
            raise ValueError("APEX Load connection claim exclusion set is invalid")
        excluded_values: list[str] = []
        for connection_id in exclude_connection_ids:
            safe_connection_id = _safe_identifier(connection_id, label="connection id")
            assert safe_connection_id is not None
            excluded_values.append(safe_connection_id)
        excluded = tuple(sorted(excluded_values))
        now = datetime.now(UTC)
        stale_before = now - NATIVE_CONNECTION_RECONCILE_LEASE
        async with self._session_factory() as session:
            native_enabled = Connection.options["native_control_enabled"].as_boolean()
            query = (
                select(Connection)
                .outerjoin(
                    ApexLoadNativeReconcileLease,
                    ApexLoadNativeReconcileLease.connection_id == Connection.id,
                )
                .where(
                    Connection.enabled.is_(True),
                    Connection.kind == "execution_engine",
                    Connection.provider == "apex_load",
                    Connection.project_id.is_not(None),
                    native_enabled.is_(True),
                    or_(
                        ApexLoadNativeReconcileLease.connection_id.is_(None),
                        and_(
                            ApexLoadNativeReconcileLease.claim_token.is_(None),
                            or_(
                                ApexLoadNativeReconcileLease.next_poll_at.is_(None),
                                ApexLoadNativeReconcileLease.next_poll_at <= now,
                            ),
                        ),
                        and_(
                            ApexLoadNativeReconcileLease.claim_token.is_not(None),
                            or_(
                                ApexLoadNativeReconcileLease.claimed_at.is_(None),
                                ApexLoadNativeReconcileLease.claimed_at <= stale_before,
                            ),
                        ),
                    ),
                )
                .order_by(Connection.id)
                .limit(limit)
                .with_for_update(of=Connection, skip_locked=True)
            )
            if excluded:
                query = query.where(Connection.id.not_in(excluded))
            rows = list((await session.scalars(query)).all())
            claims: list[NativeConnectionClaim] = []
            for row in rows:
                if row.project_id is None:
                    continue
                lease = await session.get(
                    ApexLoadNativeReconcileLease,
                    row.id,
                    with_for_update=True,
                )
                if lease is not None and (
                    lease.claimed_at is not None
                    and lease.claimed_at > stale_before
                    and lease.claim_token is not None
                ):
                    continue
                token = uuid4().hex
                if lease is None:
                    lease = ApexLoadNativeReconcileLease(
                        connection_id=row.id,
                        connection_version=row.runtime_version,
                        project_id=row.project_id,
                    )
                    session.add(lease)
                lease.connection_version = row.runtime_version
                lease.project_id = row.project_id
                lease.claim_token = token
                lease.claimed_at = now
                lease.next_poll_at = None
                claims.append(
                    NativeConnectionClaim(
                        connection_id=row.id,
                        connection_version=row.runtime_version,
                        project_id=row.project_id,
                        claim_token=token,
                    )
                )
            await session.commit()
        return claims

    async def release_connection_claim(self, claim: NativeConnectionClaim) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(ApexLoadNativeReconcileLease)
                .where(
                    ApexLoadNativeReconcileLease.connection_id == claim.connection_id,
                    ApexLoadNativeReconcileLease.connection_version == claim.connection_version,
                    ApexLoadNativeReconcileLease.claim_token == claim.claim_token,
                )
                .values(
                    claim_token=None,
                    claimed_at=None,
                    next_poll_at=datetime.now(UTC) + NATIVE_CONNECTION_POLL_DELAY,
                )
            )
            await session.commit()

    async def replay_created_resource(
        self,
        *,
        resource_kind: str,
        connection_id: str,
        connection_version: datetime,
        project_id: str,
        remote_project_id: str,
        idempotency_key: str,
        idempotency_fingerprint: str,
        scenario_remote_id: str | None = None,
    ) -> dict[str, Any] | None:
        model = self._native_resource_model(resource_kind)
        self._validate_create_mapping(
            connection_id=connection_id,
            connection_version=connection_version,
            project_id=project_id,
            remote_project_id=remote_project_id,
            idempotency_key=idempotency_key,
            idempotency_fingerprint=idempotency_fingerprint,
            scenario_remote_id=scenario_remote_id,
            require_scenario=model is ApexLoadNativeSchedule,
        )
        async with self._session_factory() as session:
            statement = select(model).where(
                model.connection_id == connection_id,
                model.connection_version == connection_version,
                model.remote_project_id == remote_project_id,
                model.idempotency_key == idempotency_key,
            )
            if model is ApexLoadNativeSchedule:
                statement = statement.where(model.scenario_remote_id == scenario_remote_id)
            row = await session.scalar(statement)
        if row is None:
            return None
        if row.idempotency_fingerprint != idempotency_fingerprint:
            raise NativeResourceIdempotencyConflictError(
                "APEX Load native create idempotency key was reused for another request"
            )
        if row.project_id != project_id or row.status == "missing":
            raise NativeResourceIdempotencyConflictError(
                "APEX Load native create idempotency target is no longer current"
            )
        return {**dict(row.snapshot or {}), "idempotency_key": idempotency_key}

    async def record_created_resource(
        self,
        *,
        resource_kind: str,
        connection_id: str,
        connection_version: datetime,
        project_id: str,
        remote_project_id: str,
        idempotency_key: str,
        idempotency_fingerprint: str,
        resource: dict[str, Any],
        scenario_remote_id: str | None = None,
    ) -> dict[str, Any]:
        model = self._native_resource_model(resource_kind)
        self._validate_create_mapping(
            connection_id=connection_id,
            connection_version=connection_version,
            project_id=project_id,
            remote_project_id=remote_project_id,
            idempotency_key=idempotency_key,
            idempotency_fingerprint=idempotency_fingerprint,
            scenario_remote_id=scenario_remote_id,
            require_scenario=model is ApexLoadNativeSchedule,
        )
        if type(resource) is not dict:
            raise ValueError("APEX Load created resource is invalid")
        remote_id = _safe_identifier(resource.get("id"), label="created resource id")
        resource_project_id = _safe_identifier(
            resource.get("project_id"), label="created resource project id"
        )
        if resource_project_id != remote_project_id:
            raise ValueError("APEX Load created resource crossed its provider project boundary")
        assert remote_id is not None
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            connection = await session.scalar(
                select(Connection).where(Connection.id == connection_id).with_for_update()
            )
            if (
                connection is None
                or not connection.enabled
                or connection.kind != "execution_engine"
                or connection.provider != "apex_load"
                or connection.project_id != project_id
                or connection.runtime_version != connection_version
            ):
                raise NativeResourceIdempotencyConflictError(
                    "APEX Load connection changed during native resource creation"
                )
            key_statement = select(model).where(
                model.connection_id == connection_id,
                model.connection_version == connection_version,
                model.remote_project_id == remote_project_id,
                model.idempotency_key == idempotency_key,
            )
            if model is ApexLoadNativeSchedule:
                key_statement = key_statement.where(model.scenario_remote_id == scenario_remote_id)
            key_row = await session.scalar(key_statement.with_for_update())
            if key_row is not None and (
                key_row.remote_id != remote_id
                or key_row.idempotency_fingerprint != idempotency_fingerprint
            ):
                raise NativeResourceIdempotencyConflictError(
                    "APEX Load native create idempotency key resolved to another resource"
                )
            row = key_row or await session.scalar(
                select(model)
                .where(
                    model.connection_id == connection_id,
                    model.connection_version == connection_version,
                    model.remote_project_id == remote_project_id,
                    model.remote_id == remote_id,
                )
                .with_for_update()
            )
            if row is None:
                values: dict[str, Any] = {
                    "connection_id": connection_id,
                    "connection_version": connection_version,
                    "project_id": project_id,
                    "remote_project_id": remote_project_id,
                    "remote_id": remote_id,
                }
                if model is ApexLoadNativeSchedule:
                    values["scenario_remote_id"] = scenario_remote_id
                row = model(**values)
                session.add(row)
            elif row.idempotency_key not in {None, idempotency_key}:
                raise NativeResourceIdempotencyConflictError(
                    "APEX Load native resource already belongs to another create key"
                )
            row.project_id = project_id
            row.revision = _safe_revision(resource.get("revision"))
            row.status = _safe_text(resource.get("status"), maximum=64)
            row.idempotency_key = idempotency_key
            row.idempotency_fingerprint = idempotency_fingerprint
            row.snapshot = _snapshot(resource)
            row.last_seen_at = now
            await session.commit()
        return {**dict(row.snapshot or {}), "idempotency_key": idempotency_key}

    @staticmethod
    def _native_resource_model(
        resource_kind: str,
    ) -> type[ApexLoadNativeScenario] | type[ApexLoadNativeSchedule]:
        if resource_kind == "scenario":
            return ApexLoadNativeScenario
        if resource_kind == "schedule":
            return ApexLoadNativeSchedule
        raise ValueError("APEX Load native resource kind is invalid")

    @staticmethod
    def _validate_create_mapping(
        *,
        connection_id: str,
        connection_version: datetime,
        project_id: str,
        remote_project_id: str,
        idempotency_key: str,
        idempotency_fingerprint: str,
        scenario_remote_id: str | None,
        require_scenario: bool,
    ) -> None:
        _safe_identifier(connection_id, label="connection id")
        _safe_identifier(project_id, label="project id")
        _safe_identifier(remote_project_id, label="remote project id")
        _safe_idempotency_key(idempotency_key)
        if type(connection_version) is not datetime or connection_version.tzinfo is None:
            raise ValueError("APEX Load connection version is invalid")
        if type(idempotency_fingerprint) is not str or not _FINGERPRINT.fullmatch(
            idempotency_fingerprint
        ):
            raise ValueError("APEX Load native create fingerprint is invalid")
        if require_scenario:
            _safe_identifier(scenario_remote_id, label="scenario id")
        elif scenario_remote_id is not None:
            raise ValueError("APEX Load scenario create mapping has an unexpected parent")

    async def sync_scenarios(
        self,
        *,
        connection_id: str,
        project_id: str,
        remote_project_id: str,
        connection_version: datetime,
        claim_token: str,
        scenarios: list[dict[str, Any]],
    ) -> None:
        await self._sync_resources(
            model=ApexLoadNativeScenario,
            connection_id=connection_id,
            project_id=project_id,
            remote_project_id=remote_project_id,
            connection_version=connection_version,
            claim_token=claim_token,
            values=scenarios,
            scenario_remote_id=None,
        )

    async def sync_schedules(
        self,
        *,
        connection_id: str,
        project_id: str,
        remote_project_id: str,
        connection_version: datetime,
        claim_token: str,
        scenario_remote_id: str,
        schedules: list[dict[str, Any]],
    ) -> None:
        await self._sync_resources(
            model=ApexLoadNativeSchedule,
            connection_id=connection_id,
            project_id=project_id,
            remote_project_id=remote_project_id,
            connection_version=connection_version,
            claim_token=claim_token,
            values=schedules,
            scenario_remote_id=_safe_identifier(scenario_remote_id, label="scenario id"),
        )

    async def _sync_resources(
        self,
        *,
        model: type[ApexLoadNativeScenario] | type[ApexLoadNativeSchedule],
        connection_id: str,
        project_id: str,
        remote_project_id: str,
        connection_version: datetime,
        claim_token: str,
        values: list[dict[str, Any]],
        scenario_remote_id: str | None,
    ) -> None:
        _safe_identifier(connection_id, label="connection id")
        _safe_identifier(project_id, label="project id")
        _safe_identifier(remote_project_id, label="remote project id")
        if type(connection_version) is not datetime:
            raise ValueError("APEX Load connection version is invalid")
        if type(values) is not list or len(values) > 5_000:
            raise ValueError("APEX Load resource list is invalid")
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            await self._assert_connection_claim(
                session,
                connection_id=connection_id,
                connection_version=connection_version,
                project_id=project_id,
                claim_token=claim_token,
            )
            seen: set[str] = set()
            for value in values:
                if type(value) is not dict:
                    raise ValueError("APEX Load resource is invalid")
                remote_id = _safe_identifier(value.get("id"), label="resource id")
                assert remote_id is not None
                resource_project = _safe_identifier(
                    value.get("project_id"), label="resource project id", allow_none=True
                )
                if resource_project is not None and resource_project != remote_project_id:
                    raise ValueError("APEX Load resource crossed its provider project boundary")
                seen.add(remote_id)
                row = await session.scalar(
                    select(model)
                    .where(
                        model.connection_id == connection_id,
                        model.connection_version == connection_version,
                        model.remote_project_id == remote_project_id,
                        model.remote_id == remote_id,
                    )
                    .with_for_update()
                )
                status = _safe_text(value.get("status"), maximum=64)
                revision = _safe_revision(value.get("revision"))
                idempotency_key = _safe_text(value.get("idempotency_key"), maximum=255)
                if row is None:
                    kwargs: dict[str, Any] = {
                        "connection_id": connection_id,
                        "connection_version": connection_version,
                        "project_id": project_id,
                        "remote_project_id": remote_project_id,
                        "remote_id": remote_id,
                        "revision": revision,
                        "status": status,
                        "idempotency_key": idempotency_key,
                        "snapshot": _snapshot(value),
                        "last_seen_at": now,
                    }
                    if model is ApexLoadNativeSchedule:
                        kwargs["scenario_remote_id"] = scenario_remote_id
                    session.add(model(**kwargs))
                else:
                    row.connection_version = connection_version
                    row.project_id = project_id
                    row.remote_project_id = remote_project_id
                    row.revision = revision
                    row.status = status
                    if idempotency_key is not None:
                        row.idempotency_key = idempotency_key
                    row.snapshot = _snapshot(value)
                    row.last_seen_at = now
                    if isinstance(row, ApexLoadNativeSchedule):
                        if scenario_remote_id is None:
                            raise RuntimeError("APEX Load schedule mirror has no scenario id")
                        row.scenario_remote_id = scenario_remote_id
            missing = update(model).where(
                model.connection_id == connection_id,
                model.connection_version == connection_version,
                model.remote_project_id == remote_project_id,
            )
            if model is ApexLoadNativeSchedule:
                missing = missing.where(model.scenario_remote_id == scenario_remote_id)
            if seen:
                missing = missing.where(model.remote_id.not_in(seen))
            await session.execute(missing.values(status="missing"))
            if model is ApexLoadNativeScenario:
                missing_schedules = update(ApexLoadNativeSchedule).where(
                    ApexLoadNativeSchedule.connection_id == connection_id,
                    ApexLoadNativeSchedule.connection_version == connection_version,
                    ApexLoadNativeSchedule.remote_project_id == remote_project_id,
                )
                if seen:
                    missing_schedules = missing_schedules.where(
                        ApexLoadNativeSchedule.scenario_remote_id.not_in(seen)
                    )
                await session.execute(missing_schedules.values(status="missing"))
            await session.commit()

    async def sync_runs(
        self,
        *,
        connection_id: str,
        project_id: str,
        remote_project_id: str,
        connection_version: datetime,
        claim_token: str,
        runs: list[dict[str, Any]],
        complete: bool = True,
    ) -> int:
        _safe_identifier(connection_id, label="connection id")
        _safe_identifier(project_id, label="project id")
        _safe_identifier(remote_project_id, label="remote project id")
        if type(connection_version) is not datetime:
            raise ValueError("APEX Load connection version is invalid")
        if type(runs) is not list or len(runs) > 50_000:
            raise ValueError("APEX Load run list is invalid")
        now = datetime.now(UTC)
        synced = 0
        async with self._session_factory() as session:
            await self._assert_connection_claim(
                session,
                connection_id=connection_id,
                connection_version=connection_version,
                project_id=project_id,
                claim_token=claim_token,
            )
            seen: set[str] = set()
            for value in runs:
                if type(value) is not dict:
                    raise ValueError("APEX Load run is invalid")
                remote_id = _safe_identifier(value.get("id"), label="run id")
                assert remote_id is not None
                seen.add(remote_id)
                run_project = _safe_identifier(
                    value.get("project_id"), label="run project id", allow_none=True
                )
                if run_project is not None and run_project != remote_project_id:
                    raise ValueError("APEX Load run crossed its provider project boundary")
                raw_status = _safe_text(value.get("status"), maximum=64)
                if raw_status is None:
                    raise ValueError("APEX Load run status is invalid")
                status = raw_status.upper()
                row = await session.scalar(
                    select(ApexLoadNativeRun)
                    .where(
                        ApexLoadNativeRun.connection_id == connection_id,
                        ApexLoadNativeRun.connection_version == connection_version,
                        ApexLoadNativeRun.remote_project_id == remote_project_id,
                        ApexLoadNativeRun.remote_id == remote_id,
                    )
                    .with_for_update()
                )
                values = {
                    "connection_version": connection_version,
                    "project_id": project_id,
                    "remote_project_id": remote_project_id,
                    "scenario_remote_id": _safe_identifier(
                        value.get("scenario_id"), label="run scenario id", allow_none=True
                    ),
                    "schedule_remote_id": _safe_identifier(
                        value.get("schedule_id"), label="run schedule id", allow_none=True
                    ),
                    "revision": _safe_revision(value.get("revision")),
                    "status": status,
                    "terminal": status in _TERMINAL_STATUSES,
                    "started_at": _safe_timestamp(value.get("started_at")),
                    "ended_at": _safe_timestamp(value.get("ended_at")),
                    "summary": _snapshot(value),
                    "last_seen_at": now,
                }
                if row is None:
                    session.add(
                        ApexLoadNativeRun(
                            connection_id=connection_id,
                            remote_id=remote_id,
                            idempotency_key=native_run_idempotency_key(
                                connection_id,
                                connection_version,
                                remote_project_id,
                                remote_id,
                            ),
                            **values,
                        )
                    )
                else:
                    for field, item in values.items():
                        setattr(row, field, item)
                    if (
                        row.pipeline_status == "stale"
                        and row.pipeline_launch_error == "native_run_missing"
                    ):
                        row.pipeline_status = "pending"
                        row.pipeline_launch_error = None
                synced += 1
            if complete:
                missing = update(ApexLoadNativeRun).where(
                    ApexLoadNativeRun.connection_id == connection_id,
                    ApexLoadNativeRun.connection_version == connection_version,
                    ApexLoadNativeRun.remote_project_id == remote_project_id,
                    ApexLoadNativeRun.pipeline_status != "launching",
                )
                if seen:
                    missing = missing.where(ApexLoadNativeRun.remote_id.not_in(seen))
                await session.execute(
                    missing.values(
                        status="missing",
                        terminal=False,
                        pipeline_status=case(
                            (
                                ApexLoadNativeRun.pipeline_status == "launched",
                                ApexLoadNativeRun.pipeline_status,
                            ),
                            else_="stale",
                        ),
                        claim_token=None,
                        claimed_at=None,
                        pipeline_launch_error=case(
                            (
                                ApexLoadNativeRun.pipeline_status == "launched",
                                ApexLoadNativeRun.pipeline_launch_error,
                            ),
                            else_="native_run_missing",
                        ),
                    )
                )
            await session.commit()
        return synced

    async def claim_terminal_runs(
        self,
        *,
        limit: int = 100,
        exclude_run_ids: frozenset[str] = frozenset(),
    ) -> list[NativeRunLaunchClaim]:
        if not 1 <= limit <= 500:
            raise ValueError("APEX Load launch claim limit is invalid")
        if not isinstance(exclude_run_ids, frozenset) or len(exclude_run_ids) > 500:
            raise ValueError("APEX Load launch claim exclusion set is invalid")
        excluded_values: list[str] = []
        for run_id in exclude_run_ids:
            safe_run_id = _safe_identifier(run_id, label="native run mirror id")
            assert safe_run_id is not None
            excluded_values.append(safe_run_id)
        now = datetime.now(UTC)
        stale_before = now - NATIVE_RUN_LAUNCH_LEASE
        async with self._session_factory() as session:
            query = (
                select(ApexLoadNativeRun)
                .where(
                    ApexLoadNativeRun.terminal.is_(True),
                    or_(
                        and_(
                            ApexLoadNativeRun.pipeline_status.in_(("pending", "retry")),
                            ApexLoadNativeRun.claimed_at.is_(None),
                        ),
                        and_(
                            ApexLoadNativeRun.pipeline_status == "launching",
                            ApexLoadNativeRun.claimed_at <= stale_before,
                        ),
                    ),
                )
                .order_by(ApexLoadNativeRun.ended_at, ApexLoadNativeRun.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            if excluded_values:
                query = query.where(ApexLoadNativeRun.id.not_in(tuple(sorted(excluded_values))))
            rows = list((await session.scalars(query)).all())
            claims: list[NativeRunLaunchClaim] = []
            for row in rows:
                token = uuid4().hex
                row.pipeline_status = "launching"
                row.claim_token = token
                row.claimed_at = now
                row.pipeline_launch_error = None
                claims.append(
                    NativeRunLaunchClaim(
                        id=row.id,
                        connection_id=row.connection_id,
                        connection_version=row.connection_version,
                        project_id=row.project_id,
                        remote_project_id=row.remote_project_id,
                        remote_id=row.remote_id,
                        status=row.status,
                        summary=dict(row.summary or {}),
                        idempotency_key=row.idempotency_key,
                        claim_token=token,
                    )
                )
            await session.commit()
        return claims

    async def mark_pipeline_launched(
        self, claim: NativeRunLaunchClaim, *, thread_id: str, run_id: str
    ) -> bool:
        _safe_identifier(thread_id, label="pipeline thread id")
        _safe_identifier(run_id, label="pipeline run id")
        async with self._session_factory() as session:
            result = await session.execute(
                update(ApexLoadNativeRun)
                .where(
                    ApexLoadNativeRun.id == claim.id,
                    ApexLoadNativeRun.claim_token == claim.claim_token,
                    ApexLoadNativeRun.pipeline_status == "launching",
                )
                .values(
                    pipeline_status="launched",
                    pipeline_thread_id=thread_id,
                    pipeline_run_id=run_id,
                    claim_token=None,
                    claimed_at=None,
                    pipeline_launch_error=None,
                )
            )
            await session.commit()
        return bool(getattr(result, "rowcount", 0))

    async def release_launch_claim(self, claim: NativeRunLaunchClaim, *, error_kind: str) -> None:
        safe_error = _safe_text(error_kind, maximum=255) or "provider_error"
        async with self._session_factory() as session:
            await session.execute(
                update(ApexLoadNativeRun)
                .where(
                    ApexLoadNativeRun.id == claim.id,
                    ApexLoadNativeRun.claim_token == claim.claim_token,
                    ApexLoadNativeRun.pipeline_status == "launching",
                )
                .values(
                    pipeline_status="retry",
                    claim_token=None,
                    claimed_at=None,
                    pipeline_launch_error=safe_error,
                )
            )
            await session.commit()

    async def mark_launch_claim_stale(
        self, claim: NativeRunLaunchClaim, *, error_kind: str
    ) -> None:
        safe_error = _safe_text(error_kind, maximum=255) or "connection_drift"
        async with self._session_factory() as session:
            await session.execute(
                update(ApexLoadNativeRun)
                .where(
                    ApexLoadNativeRun.id == claim.id,
                    ApexLoadNativeRun.claim_token == claim.claim_token,
                    ApexLoadNativeRun.pipeline_status == "launching",
                )
                .values(
                    pipeline_status="stale",
                    claim_token=None,
                    claimed_at=None,
                    pipeline_launch_error=safe_error,
                )
            )
            await session.commit()

    async def list_runs(
        self, *, connection_id: str, project_id: str, limit: int, offset: int
    ) -> list[ApexLoadNativeRun]:
        _safe_identifier(connection_id, label="connection id")
        _safe_identifier(project_id, label="project id")
        if not 1 <= limit <= 500 or not 0 <= offset <= 10_000:
            raise ValueError("APEX Load native run pagination is invalid")
        async with self._session_factory() as session:
            return list(
                (
                    await session.scalars(
                        select(ApexLoadNativeRun)
                        .where(
                            ApexLoadNativeRun.connection_id == connection_id,
                            ApexLoadNativeRun.project_id == project_id,
                        )
                        .order_by(ApexLoadNativeRun.started_at.desc(), ApexLoadNativeRun.id.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                ).all()
            )

    @staticmethod
    async def _assert_connection_claim(
        session: AsyncSession,
        *,
        connection_id: str,
        connection_version: datetime,
        project_id: str,
        claim_token: str,
    ) -> None:
        lease = await session.scalar(
            select(ApexLoadNativeReconcileLease)
            .where(ApexLoadNativeReconcileLease.connection_id == connection_id)
            .with_for_update()
        )
        if (
            lease is None
            or lease.connection_version != connection_version
            or lease.project_id != project_id
            or lease.claim_token != claim_token
        ):
            raise ValueError("APEX Load reconciliation claim is no longer current")
