import pytest

from apex.domain.cycles import CycleAction, VersionCreate, add_version, transition
from apex.domain.deliverables import DELIVERABLE_FIELDS, content_issues


def test_incomplete_draft_is_saveable_but_cannot_be_submitted():
    draft = add_version(
        [],
        VersionCreate(
            expected_revision=1,
            kind="test_plan",
            title="Plan",
            content={"objectives": "Latency"},
            change_summary="Start",
        ),
        "engineer",
    )
    assert draft["status"] == "draft"
    with pytest.raises(ValueError, match="Concurrent users is required"):
        transition(
            [draft],
            CycleAction(expected_revision=1, version_id=draft["id"], action="submit"),
            "engineer",
            independent_review=False,
        )


def test_script_engine_format_and_native_source_validation():
    content = {field.key: "Recorded" for field in DELIVERABLE_FIELDS["script"]}
    content.update(engine="apex-load", format="web_http_html")
    assert "The script format does not match the execution platform" in content_issues(
        "script", content
    )
    content.update(format="apex_native_json", source="not json")
    assert "APEX native script source must be a non-empty JSON object" in content_issues(
        "script", content
    )
    content["source"] = '{"name":"checkout", "steps":[]}'
    assert content_issues("script", content) == []


@pytest.mark.parametrize("value", [-1, True, "100000", 1_000_001])
def test_workload_controls_reject_invalid_values(value):
    assert any(
        "Concurrent users must be between" in issue
        for issue in content_issues("test_plan", {"vusers": value})
    )


def test_legacy_generic_approval_is_not_current_for_structured_review():
    from apex.domain.cycles import approval_current

    draft = add_version(
        [],
        VersionCreate(
            expected_revision=1,
            kind="test_plan",
            title="Legacy plan",
            content={"objectives": "Latency"},
            change_summary="Legacy",
        ),
        "engineer",
    )
    draft["status"] = "approved"
    assert not approval_current([draft], draft["id"])


@pytest.mark.parametrize(
    "kind,key,label",
    [
        ("test_plan", "vusers", "Concurrent users"),
        ("readiness", "target_version", "Approved target version"),
    ],
)
def test_integer_deliverable_fields_reject_fractional_values(kind, key, label):
    assert f"{label} must be a whole number" in content_issues(kind, {key: 12.75})
    assert not any(label in issue for issue in content_issues(kind, {key: 12}))
    assert not any(label in issue for issue in content_issues(kind, {key: 12.0}))
    assert next(field for field in DELIVERABLE_FIELDS[kind] if field.key == key).integer


def test_readiness_fields_explain_evidence_and_arrival_rate_limit():
    assert all(field.help for field in DELIVERABLE_FIELDS["readiness"])
    arrival = next(
        field for field in DELIVERABLE_FIELDS["test_plan"] if field.key == "arrival_rate"
    )
    assert "not supported" in arrival.help
