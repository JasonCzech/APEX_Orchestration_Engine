"""Database-backed query and mutation coverage for engine-run history."""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from apex.auth.identity import ScopeRef
from apex.persistence.models import Base, EngineRun
from apex.persistence.repositories.engine_runs import EngineRunsRepository


class _AsyncFacade:
    def __init__(self, session: Session) -> None:
        self._session = session

    async def scalar(self, statement: Any) -> Any:
        return self._session.scalar(statement)

    async def scalars(self, statement: Any) -> Any:
        return self._session.scalars(statement)

    async def execute(self, statement: Any) -> Any:
        return self._session.execute(statement)

    async def commit(self) -> None:
        self._session.commit()

    async def rollback(self) -> None:
        self._session.rollback()

    def in_transaction(self) -> bool:
        return self._session.in_transaction()


def _run(
    run_id: str,
    *,
    thread_id: str,
    attempt: int,
    project_id: str,
    app_id: str | None,
    status: str = "running",
    ownership_known: bool = True,
    scope_ownership_known: bool = True,
    external_run_id: str | None = None,
) -> EngineRun:
    return EngineRun(
        id=run_id * 32,
        thread_id=thread_id,
        project_id=project_id,
        app_id=app_id,
        ownership_known=ownership_known,
        scope_ownership_known=scope_ownership_known,
        attempt=attempt,
        engine="sim",
        external_run_id=external_run_id,
        artifact_namespace=f"runs/{thread_id}/{attempt}/{run_id}",
        handle={},
        status=status,
        started_at=datetime.now(UTC) + timedelta(seconds=attempt),
    )


async def test_engine_run_queries_enforce_app_and_project_visibility() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        Base.metadata.tables["apex.connections"].create(connection)
        Base.metadata.tables["apex.engine_runs"].create(connection)
    try:
        with Session(engine, expire_on_commit=False) as session:
            rows = [
                _run("1", thread_id="thread-p1", attempt=1, project_id="p1", app_id=None),
                _run(
                    "2",
                    thread_id="thread-p1",
                    attempt=2,
                    project_id="p1",
                    app_id="app-a",
                    external_run_id="external-a",
                ),
                _run(
                    "3",
                    thread_id="thread-p1",
                    attempt=3,
                    project_id="p1",
                    app_id="app-b",
                    status="completed",
                ),
                _run(
                    "4",
                    thread_id="thread-old",
                    attempt=1,
                    project_id="p1",
                    app_id=None,
                    ownership_known=False,
                    scope_ownership_known=False,
                ),
                _run("5", thread_id="thread-p2", attempt=1, project_id="p2", app_id="app-c"),
            ]
            session.add_all(rows)
            session.commit()
            repository = EngineRunsRepository(cast(AsyncSession, _AsyncFacade(session)))

            app_scope = [ScopeRef(project_id="p1", app_id="app-a")]
            visible, total = await repository.list_runs(
                engine="sim",
                status="running",
                allowed_scopes=app_scope,
                limit=10,
                offset=0,
            )
            assert total == 2
            assert {row.id for row in visible} == {"1" * 32, "2" * 32}

            project_visible, total = await repository.list_runs(
                allowed_scopes=[ScopeRef(project_id="p1")]
            )
            assert total == 4
            assert {row.id for row in project_visible} == {
                "1" * 32,
                "2" * 32,
                "3" * 32,
                "4" * 32,
            }
            assert await repository.list_runs(allowed_scopes=[]) == ([], 0)
            p2, total = await repository.list_runs(allowed_project_ids=("p2",))
            assert total == 1 and p2[0].id == "5" * 32
            assert await repository.list_runs(allowed_project_ids=()) == ([], 0)

            thread_rows = await repository.list_for_thread(
                "thread-p1", allowed_scopes=app_scope, limit=10, offset=0
            )
            assert [row.attempt for row in thread_rows] == [2, 1]
            assert (
                await repository.get_latest_for_thread("thread-p1", allowed_scopes=app_scope)
            ).attempt == 2  # type: ignore[union-attr]
            assert await repository.get_latest_for_thread("missing") is None

            abortable = await repository.get_latest_abortable_for_thread(
                "thread-p1", allowed_scopes=app_scope
            )
            assert abortable is not None and abortable.id == "2" * 32
            assert (
                await repository.get_latest_abortable_for_thread(
                    "thread-p1", allowed_scopes=[ScopeRef(project_id="p1")]
                )
            ).id == "2" * 32  # type: ignore[union-attr]
            assert (
                await repository.get_latest_abortable_for_thread(
                    "thread-p2", allowed_project_ids=("p2",)
                )
            ).id == "5" * 32  # type: ignore[union-attr]

            assert (
                await repository.get_by_external_run_id("external-a", allowed_scopes=app_scope)
            ).id == "2" * 32  # type: ignore[union-attr]
            assert (
                await repository.get_by_artifact_namespace(
                    f"{rows[1].artifact_namespace}/", allowed_scopes=app_scope
                )
            ).id == "2" * 32  # type: ignore[union-attr]

            # Every read above opens a SQLite transaction; explicitly release it
            # before the repository's externally coordinated abort path.
            await repository.release_read_transaction()
            assert not session.in_transaction()
            await repository.release_read_transaction()

            changed = await repository.mark_terminal(
                "thread-p1",
                "failed",
                projection_id="2" * 32,
                attempt=2,
                expected_external_run_id="external-a",
                allowed_scopes=app_scope,
            )
            assert changed == 1
            session.expire_all()
            assert session.get(EngineRun, "2" * 32).status == "failed"  # type: ignore[union-attr]

            changed = await repository.mark_aborted(
                "thread-p2",
                projection_id="5" * 32,
                attempt=1,
                expected_external_run_id=None,
                allowed_project_ids=("p2",),
            )
            assert changed == 1

            try:
                await repository.mark_terminal(
                    "thread-p1",
                    "running",
                    projection_id="1" * 32,
                    attempt=1,
                    expected_external_run_id=None,
                )
            except ValueError as exc:
                assert "nonterminal" in str(exc)
            else:  # pragma: no cover - assertion guard
                raise AssertionError("nonterminal update unexpectedly accepted")
    finally:
        engine.dispose()
