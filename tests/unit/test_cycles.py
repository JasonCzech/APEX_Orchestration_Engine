"""Lifecycle version trust and superseded approval regressions."""

from copy import deepcopy

import pytest

from apex.domain.cycles import CycleAction, VersionCreate, add_version, transition, version_digest


def version(versions=None, **overrides):
    return add_version(
        versions or [],
        VersionCreate(
            expected_revision=1,
            kind="test_plan",
            title="Plan",
            content={
                **{
                    key: "Documented"
                    for key in (
                        "objectives",
                        "journeys",
                        "workload_mix",
                        "test_data",
                        "thresholds",
                        "observability",
                        "entry_criteria",
                        "exit_criteria",
                    )
                },
                "vusers": 10,
                "ramp_s": 10,
                "duration_s": 60,
            },
            change_summary="Initial plan",
            **overrides,
        ),
        "author",
    )


def act(versions, target, action, actor="reviewer", independent=True):
    return transition(
        versions,
        CycleAction(expected_revision=1, version_id=target["id"], action=action),
        actor,
        independent_review=independent,
    )


def test_approved_edit_is_a_new_draft_and_old_evidence_is_immutable():
    initial = version()
    versions = act([initial], initial, "submit", "author")
    versions = act(versions, initial, "approve")
    old = deepcopy(versions)
    replacement = version(versions, replaces=initial["id"])
    assert replacement["status"] == "draft"
    assert versions == old
    assert replacement["id"] != initial["id"]
    with pytest.raises(ValueError, match="superseded"):
        act([*versions, replacement], initial, "submit")


def test_independent_review_uses_authenticated_principal():
    initial = version()
    versions = act([initial], initial, "submit", "author")
    with pytest.raises(ValueError, match="distinct authenticated"):
        act(versions, initial, "approve", "author")
    assert act(versions, initial, "approve", "reviewer")[0]["status"] == "approved"
    assert act(versions, initial, "approve", "author", independent=False)[0]["status"] == "approved"


def test_upstream_change_invalidates_pending_approval():
    upstream = version()
    versions = act([upstream], upstream, "submit")
    versions = act(versions, upstream, "approve")
    downstream = version(versions, dependencies=[upstream["id"]])
    versions = act([*versions, downstream], downstream, "submit")
    replacement = version(versions, replaces=upstream["id"])
    with pytest.raises(ValueError, match="stale"):
        act([*versions, replacement], downstream, "approve")


def test_archive_is_reversible_and_preserves_approval_history():
    initial = version()
    approved = act(act([initial], initial, "submit"), initial, "approve")
    archived = act(approved, initial, "archive")
    restored = act(archived, initial, "restore")
    assert restored[0]["content"] == initial["content"]
    assert restored[0]["status"] == "approved"
    assert len(restored[0]["reviews"]) == 4
    assert not restored[0]["archived"]


def test_digest_is_stable_across_key_order_and_sensitive_to_content():
    assert version_digest({"a": 1, "b": 2}) == version_digest({"b": 2, "a": 1})
    assert version_digest({"a": 1}) != version_digest({"a": 2})


def test_transitive_approvals_are_qualified_after_upstream_change():
    from apex.domain.cycles import approval_current, cycle_blockers

    scope = version()
    versions = act(act([scope], scope, "submit"), scope, "approve")
    script = version(versions, dependencies=[scope["id"]])
    versions = act(act([*versions, script], script, "submit"), script, "approve")
    report = version(versions, dependencies=[script["id"]])
    versions = act(act([*versions, report], report, "submit"), report, "approve")
    assert approval_current(versions, report["id"])
    versions.append(version(versions, replaces=scope["id"]))
    assert not approval_current(versions, script["id"])
    assert not approval_current(versions, report["id"])
    assert "Approve an active test plan version" in cycle_blockers(versions)


def test_corrupted_version_content_cannot_be_submitted_or_remain_approved():
    from apex.domain.cycles import approval_current

    initial = version()
    versions = act(act([initial], initial, "submit", "author"), initial, "approve")
    versions[0]["content"]["objectives"] = "Changed outside the version API"
    assert not approval_current(versions, initial["id"])
    initial["content"]["objectives"] = "Changed outside the version API"
    with pytest.raises(ValueError, match="checksum"):
        act([initial], initial, "submit", "author")


def test_corrupted_upstream_invalidates_dependent_approval():
    from apex.domain.cycles import approval_current

    upstream = version()
    versions = act(act([upstream], upstream, "submit", "author"), upstream, "approve")
    downstream = version(versions, dependencies=[upstream["id"]])
    versions = act(
        act([*versions, downstream], downstream, "submit", "author"), downstream, "approve"
    )
    assert approval_current(versions, downstream["id"])
    versions[0]["checksum"] = "0" * 64
    assert not approval_current(versions, downstream["id"])


def test_review_rechecks_checksum_after_submission():
    initial = version()
    versions = act([initial], initial, "submit", "author")
    versions[0]["content"]["objectives"] = "Changed after submission"
    with pytest.raises(ValueError, match="checksum"):
        act(versions, initial, "approve")


def test_replacement_cannot_legitimize_corrupted_previous_evidence():
    original = version()
    original["content"]["objectives"] = "Unexpected change"
    with pytest.raises(ValueError, match="checksum"):
        version([original], replaces=original["id"])
