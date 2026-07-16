"""Shared request-boundary validation for database-compatible text and JSON."""

import pytest
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, field_validator

from apex.domain.input_limits import (
    MAX_DESCRIPTION_CHARS,
    NoNulStr,
    ScopeId,
    validate_json_object,
    validation_error_summary,
)
from apex.domain.integrations import WorkItem, WorkItemPage


@pytest.mark.parametrize("adapter", [TypeAdapter(NoNulStr), TypeAdapter(ScopeId)])
def test_database_text_types_reject_nul(adapter: TypeAdapter[str]) -> None:
    with pytest.raises(ValidationError):
        adapter.validate_python("visible\x00hidden")


@pytest.mark.parametrize(
    "value",
    [
        {"key\x00hidden": "value"},
        {"nested": ["value\x00hidden"]},
    ],
)
def test_json_objects_reject_postgres_incompatible_nul(value: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="U\\+0000"):
        validate_json_object(value, label="payload")


def test_json_object_rejects_shallow_width_before_growing_the_work_stack() -> None:
    with pytest.raises(ValueError, match="node limit"):
        validate_json_object(
            {"values": list(range(100_000))},
            label="payload",
            max_nodes=16,
        )


def test_json_object_rejects_hostile_builtin_subclasses_without_hooks() -> None:
    calls: list[str] = []

    class HostileDict(dict[object, object]):
        def items(self):  # type: ignore[no-untyped-def]
            calls.append("items")
            raise AssertionError("hostile mapping hook ran")

    class HostileString(str):
        def __len__(self) -> int:
            calls.append("len")
            raise AssertionError("hostile string hook ran")

    for payload in (HostileDict({"safe": "value"}), {"safe": HostileString("value")}):
        with pytest.raises(ValueError, match="JSON object|unsupported values"):
            validate_json_object(payload, label="payload")  # type: ignore[arg-type]

    assert calls == []


def test_json_object_rejects_cycles_oversized_integers_and_total_text() -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError, match="repeated or circular"):
        validate_json_object(cyclic, label="payload")

    with pytest.raises(ValueError, match="256 bits"):
        validate_json_object({"integer": 1 << 256}, label="payload")

    with pytest.raises(ValueError, match="byte limit"):
        validate_json_object(
            {"first": "a" * 40, "second": "b" * 40},
            label="payload",
            max_bytes=64,
        )


def test_validation_error_summary_does_not_reflect_locations_or_validator_messages() -> None:
    canary = "CANARY_REJECTED_VALIDATION_SECRET"

    class RejectingModel(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: str

        @field_validator("value")
        @classmethod
        def reject_value(cls, value: str) -> str:
            raise ValueError(f"rejected caller value {value}")

    with pytest.raises(ValidationError) as raised:
        RejectingModel.model_validate({"value": canary, canary: "extra"})

    summary = validation_error_summary(raised.value, max_chars=128)

    assert canary not in summary
    assert "rejected caller value" not in summary
    assert len(summary) <= 128


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("key", "k" * 256),
        ("title", "t" * 501),
        ("kind", "k" * 65),
        ("status", "s" * 256),
        ("description", "d" * (MAX_DESCRIPTION_CHARS + 1)),
        ("url", "u" * 4097),
    ],
)
def test_work_item_provider_response_fields_are_bounded(field: str, value: str) -> None:
    payload = {"key": "PHX-1", "title": "bounded", field: value}
    with pytest.raises(ValidationError):
        WorkItem.model_validate(payload)


def test_work_item_provider_page_is_bounded() -> None:
    item = WorkItem(key="PHX-1", title="bounded")
    with pytest.raises(ValidationError):
        WorkItemPage(items=[item] * 201, total=201)
