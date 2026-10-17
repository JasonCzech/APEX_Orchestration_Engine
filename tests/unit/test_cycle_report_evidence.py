from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from apex.domain.cycles import VersionCreate, add_version, version_digest
from apex.domain.report_evidence import FreezeReportEvidence
from apex.routers import cycle_report_evidence as api
from apex.services.cycle_results import result_view
from tests.unit.test_cycle_results import run


def report():
    return add_version(
        [],
        VersionCreate(
            expected_revision=1,
            kind="report",
            title="Report",
            content={"executive_summary": "Investigate failed threshold"},
            change_summary="Initial",
        ),
        "author",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_capture", [False, True])
async def test_capture_freezes_exact_evidence_and_replay_does_not_read_history(
    monkeypatch, previous_capture
):
    original = report()
    if previous_capture:
        original["title"] += " (evidence captured 2026-10-16 10:20:30 UTC)"
    execution = run(passed=False)
    cycle = SimpleNamespace(
        id="cycle", project_id="project", app_id="app", revision=2, versions=[original]
    )
    monkeypatch.setattr(api, "find", AsyncMock(return_value=cycle))
    save = AsyncMock()
    monkeypatch.setattr(api, "save_versions", save)
    session = AsyncMock()
    session.scalars.return_value = Mock(all=Mock(return_value=[execution]))
    body = FreezeReportEvidence(
        expected_revision=2,
        idempotency_key="capture",
        expected_results={"run": result_view(execution).evidence_checksum},
    )
    await api.freeze_evidence("cycle", original["id"], body, session, Mock(consumer_id="operator"))
    assert save.await_args is not None
    saved = save.await_args.args[3]
    draft = saved[-1]
    assert saved[0] == original
    assert draft["status"] == "draft" and draft["reviews"] == []
    assert draft["title"].startswith("Report (evidence captured ")
    assert draft["title"].endswith(" UTC)")
    assert draft["title"].count("(evidence captured ") == 1
    assert len(draft["title"]) <= 255
    assert draft["checksum"] == version_digest(draft["content"])
    assert draft["content"]["_execution_evidence"]["results"][0]["test_outcome"] == "failed"
    execution.summary["passed"] = True
    assert draft["content"]["_execution_evidence"]["results"][0]["test_outcome"] == "failed"
    cycle.versions = saved
    monkeypatch.setattr(api, "view", Mock(return_value="replayed"))
    session.scalars.reset_mock()
    assert await api.freeze_evidence("cycle", original["id"], body, session, Mock()) == "replayed"
    session.scalars.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_or_inaccessible_evidence_is_not_saved(monkeypatch):
    original = report()
    cycle = SimpleNamespace(
        id="cycle", project_id="project", app_id="app", revision=2, versions=[original]
    )
    monkeypatch.setattr(api, "find", AsyncMock(return_value=cycle))
    save = AsyncMock()
    monkeypatch.setattr(api, "save_versions", save)
    session = AsyncMock()
    session.scalars.return_value = Mock(all=Mock(return_value=[run(passed=False)]))
    body = FreezeReportEvidence(
        expected_revision=2, idempotency_key="capture", expected_results={"run": "a" * 64}
    )
    with pytest.raises(HTTPException) as error:
        await api.freeze_evidence("cycle", original["id"], body, session, Mock())
    assert error.value.status_code == 409
    save.assert_not_awaited()


@pytest.mark.parametrize("change", ["forge", "edit", "remove"])
def test_generic_edits_cannot_forge_or_modify_frozen_evidence(change):
    original = report()
    if change != "forge":
        original["content"]["_execution_evidence"] = {"results": ["frozen"]}
        original["checksum"] = version_digest(original["content"])
    content = {**original["content"]}
    if change == "remove":
        content.pop("_execution_evidence")
    else:
        content["_execution_evidence"] = {"results": ["changed"]}
    with pytest.raises(ValueError, match="Frozen evidence"):
        add_version(
            [original],
            VersionCreate(
                expected_revision=1,
                kind="report",
                title="Edit",
                content=content,
                replaces=original["id"],
                change_summary="Edit",
            ),
            "author",
        )


def test_interpretation_edit_preserves_frozen_snapshot_and_requires_review():
    original = report()
    original["content"]["_execution_evidence"] = {"results": ["frozen"]}
    original["checksum"] = version_digest(original["content"])
    original["status"] = "approved"
    revised = add_version(
        [original],
        VersionCreate(
            expected_revision=1,
            kind="report",
            title="Edit",
            replaces=original["id"],
            content={**original["content"], "executive_summary": "Updated interpretation"},
            change_summary="Clarified impact",
        ),
        "author",
    )
    assert revised["status"] == "draft"
    assert revised["reviews"] == []
    assert revised["content"]["_execution_evidence"] == original["content"]["_execution_evidence"]
    assert original["content"]["executive_summary"] == "Investigate failed threshold"
