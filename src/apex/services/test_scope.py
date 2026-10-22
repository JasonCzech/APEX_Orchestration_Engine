"""Resolve every participant before persisting a combined-system run."""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import ColumnElement

from apex.auth.identity import ScopeRef
from apex.domain.test_scope import TestScopeV1
from apex.services.environments import resolve_environment_context, resolve_environment_target


def allows_test_scope(identity, scope: TestScopeV1) -> bool:
    return all(
        identity.allows_scope(project_id=p.project_id, app_id=p.application_id)
        for p in scope.participants
    )


async def resolve_test_scope(
    repository, identity, scope: TestScopeV1, *, live: bool
) -> dict[str, Any]:
    if not allows_test_scope(identity, scope):
        raise HTTPException(403, "Access to every participating application is required")
    if live and scope.traffic_entry is None:
        raise HTTPException(422, "Select a traffic entry participant for live execution")
    participants = []
    for participant in sorted(scope.participants, key=lambda p: (p.layer, p.id)):
        environment = await resolve_environment_context(
            repository,
            participant.environment_id,
            project_id=participant.project_id,
            app_id=participant.application_id,
        )
        # Explicit allow-list: never copy environment options or credentials.
        participants.append(
            {
                **participant.model_dump(mode="json"),
                "application_name": environment.application.name,
                "application_description": environment.application.description,
                "environment_name": environment.name,
                "environment_kind": environment.kind,
                "hosts": [
                    {"hostname": host.hostname, "role": host.role}
                    for host in getattr(environment, "hosts", [])
                ],
            }
        )
    snapshot = {**scope.model_dump(mode="json"), "participants": participants}
    entry = scope.traffic_entry
    if live and entry is not None:
        target = await resolve_environment_target(
            repository,
            entry.environment_id,
            project_id=entry.project_id,
            app_id=entry.application_id,
        )
        snapshot["execution_target"] = {"base_url": target.base_url, "version": target.version}
    return snapshot


def _canonical_scope(scope: TestScopeV1) -> dict[str, Any]:
    value = scope.model_dump(mode="json")
    value["participants"] = sorted(value["participants"], key=lambda p: p["id"])
    return value


def _thread_key(value: str | UUID) -> str:
    if type(value) is UUID:
        return str(value)
    if type(value) is not str or not value or len(value) > 255 or "\x00" in value:
        raise ValueError("Invalid run identity")
    return value


def _scope_digest(scope: TestScopeV1) -> str:
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(_canonical_scope(scope), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _expected_rows(scope: TestScopeV1) -> dict[str, tuple]:
    return {
        p.id: (
            p.project_id,
            p.application_id,
            p.environment_id,
            p.layer,
            p.id == scope.primary_participant_id,
            p.id == scope.traffic_entry_participant_id,
        )
        for p in scope.participants
    }


def _actual_rows(rows) -> dict[str, tuple]:
    return {
        p.participant_id: (
            p.project_id,
            p.application_id,
            p.environment_id,
            p.layer,
            p.is_primary,
            p.is_traffic_entry,
        )
        for p in rows
    }


def _verified_binding(binding, rows) -> TestScopeV1:
    if binding is None:
        raise ValueError("Run participant binding is missing")
    scope = TestScopeV1.model_validate(binding.scope)
    if (
        binding.participant_count != len(scope.participants)
        or binding.scope_digest != _scope_digest(scope)
        or _actual_rows(rows) != _expected_rows(scope)
    ):
        raise ValueError("Run participant binding is incomplete or corrupt")
    return scope


async def persist_test_scope(thread_id: str | UUID, scope: TestScopeV1) -> None:
    thread_id = _thread_key(thread_id)
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from apex.persistence.db import get_sessionmaker
    from apex.persistence.models import RunParticipant, RunScopeBinding

    async with get_sessionmaker()() as session:
        binding = await session.get(RunScopeBinding, thread_id)
        rows = list(
            (
                await session.scalars(
                    select(RunParticipant).where(RunParticipant.thread_id == thread_id)
                )
            ).all()
        )
        if binding is not None or rows:
            existing = _verified_binding(binding, rows)
            if _canonical_scope(existing) != _canonical_scope(scope):
                raise ValueError("A run's participant scope is immutable")
            return
        session.add(
            RunScopeBinding(
                thread_id=thread_id,
                scope=_canonical_scope(scope),
                participant_count=len(scope.participants),
                scope_digest=_scope_digest(scope),
            )
        )
        for p in scope.participants:
            session.add(
                RunParticipant(
                    thread_id=thread_id,
                    participant_id=p.id,
                    project_id=p.project_id,
                    application_id=p.application_id,
                    environment_id=p.environment_id,
                    layer=p.layer,
                    is_primary=p.id == scope.primary_participant_id,
                    is_traffic_entry=p.id == scope.traffic_entry_participant_id,
                )
            )
        try:
            await session.commit()
        except IntegrityError:
            # A competing idempotent launch may have committed the same binding.
            # Re-read only after rolling back our entire header+rows transaction.
            await session.rollback()
            binding = await session.get(RunScopeBinding, thread_id)
            rows = list(
                (
                    await session.scalars(
                        select(RunParticipant).where(RunParticipant.thread_id == thread_id)
                    )
                ).all()
            )
            existing = _verified_binding(binding, rows)
            if _canonical_scope(existing) != _canonical_scope(scope):
                raise ValueError("A run's participant scope is immutable") from None


def participant_visibility(thread_column, scopes: Sequence[ScopeRef] | None) -> ColumnElement[bool]:
    """A missing member or unauthorized member hides the whole combined run."""
    from sqlalchemy import and_, exists, false, func, or_, select, true

    from apex.persistence.models import RunParticipant, RunScopeBinding

    if scopes is None:
        return true()
    allowed = (
        or_(
            *[
                and_(
                    RunParticipant.project_id == s.project_id,
                    RunParticipant.application_id == s.app_id,
                )
                if s.app_id
                else RunParticipant.project_id == s.project_id
                for s in scopes
            ]
        )
        if scopes
        else false()
    )
    members = select(1).where(RunParticipant.thread_id == thread_column)
    binding = select(1).where(RunScopeBinding.thread_id == thread_column)
    count = (
        select(func.count())
        .select_from(RunParticipant)
        .where(RunParticipant.thread_id == RunScopeBinding.thread_id)
        .correlate(RunScopeBinding)
        .scalar_subquery()
    )
    complete_binding = exists(
        binding.where(
            RunScopeBinding.participant_count > 0,
            RunScopeBinding.participant_count == count,
        )
    )
    return and_(
        ~exists(members.where(~allowed)),
        or_(complete_binding, and_(~exists(binding), ~exists(members))),
    )


_UNSPECIFIED_SCOPE = object()


async def load_persisted_test_scope(thread_id: str | UUID) -> TestScopeV1 | None:
    """Return the verified canonical binding; only an absent header AND rows is legacy.

    An orphaned, incomplete or corrupt projection raises ValueError. No database
    transaction remains open while callers perform provider or native runtime IO.
    """
    thread_id = _thread_key(thread_id)
    from sqlalchemy import select

    from apex.persistence.db import get_sessionmaker
    from apex.persistence.models import RunParticipant, RunScopeBinding

    async with get_sessionmaker()() as session:
        binding = await session.get(RunScopeBinding, thread_id)
        rows = list(
            (
                await session.scalars(
                    select(RunParticipant).where(RunParticipant.thread_id == thread_id)
                )
            ).all()
        )
    if binding is None and not rows:
        return None
    return _verified_binding(binding, rows)


async def can_access_thread(
    identity, thread_id: str | UUID, *, expected_scope=_UNSPECIFIED_SCOPE
) -> bool:
    """Verify durable scope and, when supplied, its native metadata counterpart."""
    thread_id = _thread_key(thread_id)
    return thread_id in await accessible_thread_ids(identity, {thread_id: expected_scope})


async def accessible_thread_ids(identity, expected_scopes: dict[str, Any]) -> set[str]:
    from sqlalchemy import select

    from apex.persistence.db import get_sessionmaker
    from apex.persistence.models import RunParticipant, RunScopeBinding

    if identity.is_unscoped:
        return set(expected_scopes)
    async with get_sessionmaker()() as session:
        bindings = {
            b.thread_id: b
            for b in (
                await session.scalars(
                    select(RunScopeBinding).where(RunScopeBinding.thread_id.in_(expected_scopes))
                )
            ).all()
        }
        rows: dict[str, list] = {}
        for row in (
            await session.scalars(
                select(RunParticipant).where(RunParticipant.thread_id.in_(expected_scopes))
            )
        ).all():
            rows.setdefault(row.thread_id, []).append(row)
    visible = set()
    for thread_id, expected in expected_scopes.items():
        binding = bindings.get(thread_id)
        members = rows.get(thread_id, [])
        if binding is None and not members:
            if expected is _UNSPECIFIED_SCOPE or expected is None:
                visible.add(thread_id)
            continue
        try:
            scope = _verified_binding(binding, members)
            if expected is not _UNSPECIFIED_SCOPE:
                if expected is None or _canonical_scope(
                    TestScopeV1.model_validate(expected)
                ) != _canonical_scope(scope):
                    continue
            if allows_test_scope(identity, scope):
                visible.add(thread_id)
        except (ValueError, TypeError):
            continue
    return visible


async def verify_persisted_test_scope(identity, thread_id: str | UUID, scope: TestScopeV1) -> None:
    thread_id = _thread_key(thread_id)
    if not allows_test_scope(identity, scope):
        raise HTTPException(403, "Access to every test participant is required")
    # Verification also applies to platform administrators; this is an immutable
    # launch binding check rather than merely an audience check.
    from sqlalchemy import select

    from apex.persistence.db import get_sessionmaker
    from apex.persistence.models import RunParticipant, RunScopeBinding

    async with get_sessionmaker()() as session:
        binding = await session.get(RunScopeBinding, thread_id)
        rows = list(
            (
                await session.scalars(
                    select(RunParticipant).where(RunParticipant.thread_id == thread_id)
                )
            ).all()
        )
    try:
        saved = _verified_binding(binding, rows)
        matches = _canonical_scope(saved) == _canonical_scope(scope)
    except (ValueError, TypeError):
        matches = False
    if not matches:
        raise HTTPException(
            409, "Run scope differs from the immutable thread scope; create a new test"
        )
