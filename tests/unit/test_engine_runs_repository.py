"""Engine-run repository scope predicates."""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from apex.auth.identity import ScopeRef
from apex.persistence.models import Base, EngineRun
from apex.persistence.repositories.engine_runs import _mutation_filter, _visibility_filter


def _seed_engine_runs(session: Session) -> None:
    rows = (
        ("exact", "p1", "app-a", True, True),
        ("sibling", "p1", "app-b", True, True),
        ("project-level", "p1", None, True, True),
        ("legacy-unknown", "p1", None, False, False),
        # An old 0027 pod can write after the pre-upgrade 0028 migration. It
        # explicitly sets the old bit true but cannot set the new scope bit.
        ("rolling-ambiguous", "p1", None, True, False),
        ("other-project", "p2", "app-a", True, True),
    )
    for thread_id, project_id, app_id, ownership_known, scope_ownership_known in rows:
        session.add(
            EngineRun(
                id=thread_id,
                thread_id=thread_id,
                project_id=project_id,
                app_id=app_id,
                ownership_known=ownership_known,
                scope_ownership_known=scope_ownership_known,
                attempt=1,
                engine="sim",
                handle={},
                status="running",
            )
        )
    session.commit()


def test_app_scope_hides_sibling_and_legacy_unknown_runs() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        Base.metadata.tables["apex.engine_runs"].create(connection)

    with Session(engine) as session:
        _seed_engine_runs(session)
        predicate = _visibility_filter(
            allowed_scopes=[ScopeRef(project_id="p1", app_id="app-a")],
            allowed_project_ids=None,
        )
        assert predicate is not None
        thread_ids = set(session.scalars(select(EngineRun.thread_id).where(predicate)))

    assert thread_ids == {"exact", "project-level"}


def test_project_scope_sees_all_project_runs_including_legacy_unknown() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        Base.metadata.tables["apex.engine_runs"].create(connection)

    with Session(engine) as session:
        _seed_engine_runs(session)
        predicate = _visibility_filter(
            allowed_scopes=[ScopeRef(project_id="p1")],
            allowed_project_ids=None,
        )
        assert predicate is not None
        thread_ids = set(session.scalars(select(EngineRun.thread_id).where(predicate)))

    assert thread_ids == {
        "exact",
        "sibling",
        "project-level",
        "legacy-unknown",
        "rolling-ambiguous",
    }


def test_app_scope_can_mutate_only_exact_app_runs() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        Base.metadata.tables["apex.engine_runs"].create(connection)

    with Session(engine) as session:
        _seed_engine_runs(session)
        predicate = _mutation_filter(
            allowed_scopes=[ScopeRef(project_id="p1", app_id="app-a")],
            allowed_project_ids=None,
        )
        assert predicate is not None
        thread_ids = set(session.scalars(select(EngineRun.thread_id).where(predicate)))

    assert thread_ids == {"exact"}


def test_project_scope_can_mutate_all_project_runs() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        Base.metadata.tables["apex.engine_runs"].create(connection)

    with Session(engine) as session:
        _seed_engine_runs(session)
        predicate = _mutation_filter(
            allowed_scopes=[ScopeRef(project_id="p1")],
            allowed_project_ids=None,
        )
        assert predicate is not None
        thread_ids = set(session.scalars(select(EngineRun.thread_id).where(predicate)))

    assert thread_ids == {
        "exact",
        "sibling",
        "project-level",
        "legacy-unknown",
        "rolling-ambiguous",
    }
