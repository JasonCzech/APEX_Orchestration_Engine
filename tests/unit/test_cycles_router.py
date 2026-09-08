from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.models import TestCycle as CycleRow
from apex.routers.cycles import save_versions, visibility


def test_visibility_preserves_app_scope_in_sql():
    identity = ConsumerIdentity(
        consumer_id="a",
        name="A",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="p", app_id="allowed-app")],
    )
    query = visibility(identity).compile(dialect=postgresql.dialect())
    assert "app_id" in str(query)
    assert set(query.params.values()) == {"p", "allowed-app"}
    identity.scopes = []
    assert str(visibility(identity)) == "false"


@pytest.mark.asyncio
async def test_edit_uses_database_revision_cas_and_rolls_back_conflict():
    now = datetime.now(UTC)
    row = CycleRow(
        id="cycle",
        name="Cycle",
        project_id="p",
        app_id="a",
        environment_id="e",
        owner="author",
        mode="live",
        revision=3,
        independent_review=True,
        versions=[],
        created_at=now,
        updated_at=now,
    )
    session = AsyncMock()
    session.execute.return_value = Mock(scalar_one_or_none=Mock(return_value=None))
    with pytest.raises(HTTPException) as error:
        await save_versions(session, row, 2, [])
    assert error.value.status_code == 409
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    statement = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert "revision =" in str(statement)
    assert statement.params["revision_1"] == 2
