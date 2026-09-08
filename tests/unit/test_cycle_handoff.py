from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from apex.domain.cycle_handoff import (
    FollowupWrite,
    NextCycleRequest,
    reuse_actions,
    reuse_drafts,
    write_followup,
)
from apex.domain.cycles import approval_current
from apex.routers import cycle_handoff as api
from tests.unit.test_cycles import act, version


def test_reuse_remaps_dependencies_without_approvals_or_original_mutation():
    scope = version()
    approved = act(act([scope], scope, "submit", "author"), scope, "approve")
    downstream = version(approved, dependencies=[scope["id"]])
    original = [*approved, downstream]
    saved = deepcopy(original)
    drafts = reuse_drafts(original, [scope["id"], downstream["id"]], "new-author", "parent")
    assert original == saved
    assert drafts[1]["dependencies"] == [drafts[0]["id"]]
    assert all(d["status"] == "draft" and not d["reviews"] for d in drafts)
    assert not any(approval_current(drafts, d["id"]) for d in drafts)
    assert drafts[0]["origin"]["version_id"] == scope["id"]
    with pytest.raises(ValueError, match="upstream"):
        reuse_drafts(original, [downstream["id"]], "actor", "parent")


@pytest.mark.parametrize("change", ["archived", "corrupt", "readiness"])
def test_reuse_rejects_stale_or_operational_deliverables(change):
    source = version()
    if change == "archived":
        source["archived"] = True
    if change == "corrupt":
        source["checksum"] = "bad"
    if change == "readiness":
        source["kind"] = "readiness"
    with pytest.raises(ValueError):
        reuse_drafts([source], [source["id"]], "actor", "parent")


def test_actions_are_audited_idempotent_and_carry_only_unresolved_work():
    body = FollowupWrite(
        expected_revision=1, idempotency_key="one", title="Fix data", owner="Engineer"
    )
    actions, replay = write_followup([], body, "actor")
    assert not replay
    repeated, replay = write_followup(actions, body, "actor")
    assert replay and repeated == actions
    with pytest.raises(ValueError, match="Operation key"):
        write_followup(actions, body.model_copy(update={"title": "Changed"}), "actor")
    closed, _ = write_followup(
        actions,
        body.model_copy(
            update={"action_id": actions[0]["id"], "idempotency_key": "two", "status": "done"}
        ),
        "reviewer",
    )
    assert actions[0]["status"] == "open"
    assert len(closed[0]["history"]) == 2
    assert reuse_actions(closed, "parent", "actor") == []
    carried = reuse_actions(actions, "parent", "actor")
    assert carried[0]["origin"]["action_id"] == actions[0]["id"]
    assert carried[0]["id"] != actions[0]["id"]
    assert carried[0]["status"] == "open"


@pytest.mark.asyncio
async def test_next_cycle_is_atomic_and_never_copies_operational_rows(monkeypatch):
    parent = SimpleNamespace(
        id="parent",
        project_id="p",
        app_id="a",
        environment_id="e",
        mode="live",
        independent_review=True,
        versions=[],
        followups=[],
    )
    monkeypatch.setattr(api, "find", AsyncMock(return_value=parent))
    monkeypatch.setattr(api, "view", Mock(side_effect=lambda row, actor: row))
    session = AsyncMock()
    session.add = Mock()
    session.scalar.side_effect = [None, "target", "parent"]
    body = NextCycleRequest(expected_revision=3, idempotency_key="next", name="Next")
    child = await api.next_cycle("parent", body, session, Mock(consumer_id="operator"))
    assert child.origin_cycle_id == "parent" and child.independent_review
    assert child.revision == 1 and child.versions == []
    assert session.add.call_args.args[0] is child
    session.commit.assert_awaited_once()
    statement = session.scalar.await_args_list[-1].args[0]
    assert statement.compile().params["revision_1"] == 3


@pytest.mark.asyncio
async def test_conflicting_handoff_does_not_add_child(monkeypatch):
    parent = SimpleNamespace(
        id="parent",
        project_id="p",
        app_id="a",
        environment_id="e",
        mode="live",
        independent_review=False,
        versions=[],
        followups=[],
    )
    monkeypatch.setattr(api, "find", AsyncMock(return_value=parent))
    session = AsyncMock()
    session.add = Mock()
    session.scalar.side_effect = [None, "target", None]
    with pytest.raises(HTTPException) as error:
        await api.next_cycle(
            "parent",
            NextCycleRequest(expected_revision=1, idempotency_key="next", name="Next"),
            session,
            Mock(consumer_id="operator"),
        )
    assert error.value.status_code == 409
    session.add.assert_not_called()
    session.commit.assert_not_awaited()
