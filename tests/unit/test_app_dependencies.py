import pytest
from fastapi import HTTPException

from apex.app.dependencies import ensure_scope
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef


def _identity() -> ConsumerIdentity:
    return ConsumerIdentity(
        consumer_id="consumer-1",
        name="operator",
        consumer_type=ConsumerType.HEADLESS,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="project-1", app_id="app-1")],
    )


def test_ensure_scope_rejects_application_without_project_context() -> None:
    with pytest.raises(HTTPException) as captured:
        ensure_scope(_identity(), app_id="app-1")

    assert captured.value.status_code == 403


def test_ensure_scope_accepts_matching_project_and_application() -> None:
    ensure_scope(_identity(), project_id="project-1", app_id="app-1")
