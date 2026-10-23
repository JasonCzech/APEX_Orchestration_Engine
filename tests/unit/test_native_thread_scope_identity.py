"""Verify scope bindings against the installed native thread insertion path."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4


def test_native_thread_creation_preserves_scope_identity(tmp_path: Path) -> None:
    # Native runtime imports initialize process-global configuration and files.
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(root / "src"),
            "DATABASE_URI": ":memory:",
            "REDIS_URI": "redis://127.0.0.1:1",
            "LANGGRAPH_RUNTIME_EDITION": "inmem",
        },
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def _native_probe() -> None:
    from unittest.mock import AsyncMock, patch

    from langgraph_runtime_inmem.ops import Threads
    from langgraph_sdk import Auth
    from sqlalchemy import Table, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from apex.app.security import _direct_langgraph_body_limit_error
    from apex.auth.handlers import on_threads_create, user_payload
    from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
    from apex.domain.test_scope import TestScopeV1
    from apex.persistence.models import Base, RunParticipant, RunScopeBinding
    from apex.services.test_scope import (
        _canonical_scope,
        can_access_thread,
        load_persisted_test_scope,
    )

    engine = create_async_engine(
        "sqlite+aiosqlite://", execution_options={"schema_translate_map": {"apex": None}}
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda conn: Base.metadata.create_all(
                conn,
                tables=[
                    cast(Table, model.__table__) for model in (RunScopeBinding, RunParticipant)
                ],
            )
        )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    topology = TestScopeV1.model_validate(
        {
            "participants": [
                {
                    "id": "front",
                    "project_id": "p1",
                    "application_id": "a1",
                    "environment_id": "e1",
                    "layer": "frontend",
                },
                {
                    "id": "back",
                    "project_id": "p2",
                    "application_id": "a2",
                    "environment_id": "e2",
                    "layer": "backend",
                },
            ],
            "primary_participant_id": "front",
            "traffic_entry_participant_id": "front",
        }
    )
    identity = ConsumerIdentity(
        consumer_id="native-review",
        name="Native review",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="p1", app_id="a1"), ScopeRef(project_id="p2", app_id="a2")],
    )
    connection = SimpleNamespace(store={"threads": []})

    async def dispatch(ctx: Any, action: str, value: Any) -> Any:
        assert action == "create"
        return await on_threads_create(ctx=ctx, value=value)

    with (
        patch("apex.persistence.db.get_sessionmaker", return_value=sessions),
        patch("apex.auth.handlers._resolve_native_test_scope", new=AsyncMock()),
        patch(
            "apex.auth.handlers._load_catalog_application",
            new=AsyncMock(
                return_value=SimpleNamespace(id="a1", project_ids=["p1"], archived_at=None)
            ),
        ),
        patch.object(Threads, "handle_event", new=dispatch),
    ):
        for trusted in (False, True):
            # Public IDs originate in the route; loopback IDs may be deterministic.
            thread_id = uuid4()
            ctx = Auth.types.AuthContext(
                user=cast(Any, user_payload(identity, trusted_loopback=trusted)),
                permissions=[],
                resource="threads",
                action="create",
            )
            records = await Threads.put(
                connection,
                thread_id,
                metadata={"test_scope": topology.model_dump(mode="json")},
                if_exists="raise",
                ctx=ctx,
            )
            inserted = [record async for record in records]
            assert inserted[0]["thread_id"] == thread_id
            metadata = cast(dict[str, Any], inserted[0]["metadata"])
            assert metadata["project_id"] == "p1"
            assert metadata["app_id"] == "a1"
            persisted = await load_persisted_test_scope(thread_id)
            assert persisted is not None
            assert _canonical_scope(persisted) == _canonical_scope(topology)
            assert await can_access_thread(
                identity, thread_id, expected_scope=topology.model_dump()
            )
            async with sessions() as session:
                rows = list(
                    await session.scalars(
                        select(RunParticipant).where(RunParticipant.thread_id == str(thread_id))
                    )
                )
                assert {row.participant_id for row in rows} == {"front", "back"}

            # No rejected request may leave an orphan binding or inserted thread.
            rejected_id = uuid4()
            for conflict in ({"project_id": "p2"}, {"app_id": "a2"}):
                try:
                    await Threads.put(
                        connection,
                        rejected_id,
                        metadata={"test_scope": topology.model_dump(mode="json"), **conflict},
                        if_exists="raise",
                        ctx=ctx,
                    )
                except Auth.exceptions.HTTPException as exc:
                    assert exc.status_code == 422
                else:
                    raise AssertionError("Conflicting primary metadata was accepted")
                assert await load_persisted_test_scope(rejected_id) is None
                assert all(row["thread_id"] != rejected_id for row in connection.store["threads"])

        for payload in ({"thread_id": str(uuid4())}, {"if_exists": "do_nothing"}):
            assert (
                _direct_langgraph_body_limit_error(
                    "thread_create", json.dumps(payload).encode(), trusted_loopback=False
                )
                is not None
            )
            assert (
                _direct_langgraph_body_limit_error(
                    "thread_create", json.dumps(payload).encode(), trusted_loopback=True
                )
                is None
            )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_native_probe())
