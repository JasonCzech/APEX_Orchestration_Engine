"""Exact project/app visibility for both analytics event projections."""

from typing import Any, cast

import pytest
from sqlalchemy import Table, create_engine, insert, select

from apex.auth.identity import ScopeRef
from apex.persistence.models import AgentEvent, UsageEvent
from apex.services.analytics_scope import analytics_scope_filter


def _values(model: type[UsageEvent] | type[AgentEvent]) -> list[dict[str, Any]]:
    rows = [
        {"id": "global", "project_id": None, "app_id": None},
        {"id": "legacy", "project_id": "project-a", "app_id": None},
        {"id": "app-a", "project_id": "project-a", "app_id": "app-a"},
        {"id": "app-b", "project_id": "project-a", "app_id": "app-b"},
        {"id": "other", "project_id": "project-b", "app_id": "app-a"},
    ]
    required = (
        {"consumer_name": "consumer", "surface": "v1", "action": "read", "status": "ok"}
        if model is UsageEvent
        else {"phase": "execution", "agent_name": "worker", "status": "ok"}
    )
    return [row | required for row in rows]


def _visible_ids(
    model: type[UsageEvent] | type[AgentEvent],
    scopes: tuple[ScopeRef, ...] | None,
) -> set[str]:
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
            cast(Table, model.__table__).create(connection)
            connection.execute(insert(model), _values(model))
            predicate = analytics_scope_filter(model, scopes)
            statement = select(model.id)
            if predicate is not None:
                statement = statement.where(predicate)
            return set(connection.scalars(statement))
    finally:
        engine.dispose()


@pytest.mark.parametrize("model", [UsageEvent, AgentEvent])
def test_unscoped_admin_can_read_global_and_scoped_rows(
    model: type[UsageEvent] | type[AgentEvent],
) -> None:
    assert _visible_ids(model, None) == {"global", "legacy", "app-a", "app-b", "other"}


@pytest.mark.parametrize("model", [UsageEvent, AgentEvent])
def test_project_wide_scope_reads_all_apps_but_not_global(
    model: type[UsageEvent] | type[AgentEvent],
) -> None:
    scopes = (ScopeRef(project_id="project-a"),)
    assert _visible_ids(model, scopes) == {"legacy", "app-a", "app-b"}


@pytest.mark.parametrize("model", [UsageEvent, AgentEvent])
def test_app_scope_hides_sibling_global_and_legacy_null_app_rows(
    model: type[UsageEvent] | type[AgentEvent],
) -> None:
    scopes = (ScopeRef(project_id="project-a", app_id="app-a"),)
    assert _visible_ids(model, scopes) == {"app-a"}


@pytest.mark.parametrize("model", [UsageEvent, AgentEvent])
def test_empty_scope_set_reads_nothing(
    model: type[UsageEvent] | type[AgentEvent],
) -> None:
    assert _visible_ids(model, ()) == set()
