"""Fail-closed validation of provider work items, including the triage fields."""

from typing import Any

import pytest

from apex.domain.integrations import WorkItem
from apex.services.work_items import validated_provider_work_item

LEGACY_FIELDS = {
    "key": "PHX-241",
    "title": "Checkout p95 regression",
    "kind": "bug",
    "status": "open",
    "description": "p95 rose from 220ms to 870ms",
    "url": "https://acme.atlassian.net/browse/PHX-241",
}
TRIAGE_FIELDS = {
    "priority": "P1",
    "severity": "Sev-2",
    "sprint": "24.3",
    "assignee": "Jamie Rivera",
    "labels": ["payments", "checkout"],
    "parent_key": "PHX-100",
    "linked_keys": ["PHX-300"],
}


def test_a_legacy_six_field_row_still_validates() -> None:
    """Mutation-outbox rows persisted before the triage fields are replayed
    through this validator, so the required set must not have grown."""

    item = validated_provider_work_item(dict(LEGACY_FIELDS))
    assert item.key == "PHX-241"
    assert item.priority is None
    assert item.labels == []
    assert item.linked_keys == []


def test_a_full_item_validates_and_round_trips() -> None:
    raw: dict[str, Any] = {**LEGACY_FIELDS, **TRIAGE_FIELDS}
    item = validated_provider_work_item(raw)
    assert item.priority == "P1"
    assert item.labels == ["payments", "checkout"]
    assert item.parent_key == "PHX-100"
    assert validated_provider_work_item(item) == item


def test_credential_material_in_a_descriptive_field_is_redacted_not_reflected() -> None:
    item = validated_provider_work_item(
        {**LEGACY_FIELDS, "assignee": "Authorization: Bearer abcdef0123456789"}
    )
    assert item.assignee is not None
    assert "abcdef0123456789" not in item.assignee


@pytest.mark.parametrize(
    "override",
    [
        {"priority": 17},
        {"priority": "P" * 4_000},
        {"labels": "payments"},
        {"labels": [17]},
        {"labels": [""]},
        {"labels": ["x" * 256]},
        {"labels": [f"label-{index}" for index in range(33)]},
        {"parent_key": "not a key"},
        {"parent_key": 17},
        {"linked_keys": "PHX-300"},
        {"linked_keys": ["not a key"]},
        {"linked_keys": [f"PHX-{index}" for index in range(33)]},
    ],
)
def test_malformed_triage_fields_fail_closed(override: dict[str, Any]) -> None:
    with pytest.raises(RuntimeError, match="invalid work item"):
        validated_provider_work_item({**LEGACY_FIELDS, **override})


def test_an_unknown_field_is_still_rejected() -> None:
    with pytest.raises(RuntimeError, match="invalid work item"):
        validated_provider_work_item({**LEGACY_FIELDS, "sprint_goal": "ship it"})


def test_a_constructed_model_carrying_every_field_is_accepted() -> None:
    item = WorkItem(**LEGACY_FIELDS, **TRIAGE_FIELDS)  # type: ignore[arg-type]
    assert validated_provider_work_item(item) == item


def test_a_credential_shaped_label_fails_the_whole_item_closed() -> None:
    """Labels are fail-closed, unlike the repairable descriptive fields: a
    credential-bearing label must never reach APIs, checkpoints, or JSONB."""

    with pytest.raises(RuntimeError, match="invalid work item"):
        validated_provider_work_item(
            {**LEGACY_FIELDS, "labels": ["Authorization: Bearer abcdef0123456789"]}
        )
