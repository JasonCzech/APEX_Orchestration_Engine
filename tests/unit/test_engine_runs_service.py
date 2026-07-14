"""Engine-run projection write helpers."""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from apex.persistence.models import Base, EngineRun
from apex.services.engine_runs import _upsert_statement


def test_sqlite_upsert_is_replay_safe_and_preserves_omitted_ownership_fields() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        Base.metadata.tables["apex.engine_runs"].create(connection)
        connection.execute(
            _upsert_statement(
                {
                    "thread_id": "thread-1",
                    "attempt": 1,
                    "project_id": "project-1",
                    "app_id": "app-1",
                    "ownership_known": True,
                    "engine": "sim",
                    "handle": {"idempotency_key": "key-1"},
                    "status": "running",
                },
                "sqlite",
            )
        )
        # A later partial replay updates mutable projection fields without
        # erasing ownership fields that were intentionally omitted.
        connection.execute(
            _upsert_statement(
                {
                    "thread_id": "thread-1",
                    "attempt": 1,
                    "ownership_known": True,
                    "engine": "sim",
                    "handle": {"idempotency_key": "key-1", "external_run_id": "run-1"},
                    "external_run_id": "run-1",
                    "status": "completed",
                },
                "sqlite",
            )
        )

    with Session(engine) as session:
        rows = list(session.scalars(select(EngineRun)))

    assert len(rows) == 1
    assert rows[0].project_id == "project-1"
    assert rows[0].app_id == "app-1"
    assert rows[0].external_run_id == "run-1"
    assert rows[0].status == "completed"
