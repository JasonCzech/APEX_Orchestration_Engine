"""Adversarial checks for loopback/provider identifiers used in public URLs."""

import pytest

import apex.services.public_projection as public_projection
from apex.services.public_projection import (
    native_run_stream_url,
    public_engine_handle_summary,
    public_test_result_summary,
    validated_native_identifier,
    validated_native_mapping_page,
)


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        "",
        ".",
        "..",
        "x" * 256,
        " leading-space",
        "trailing-space ",
        "thread/escape",
        "thread%2Fescape",
        "thread?query",
        "thread#fragment",
        "thread\\escape",
        "thread-é",
        "thread\u202etxt",
        "password=native-id-secret-canary",
        "Authorization: Bearer native-id-secret-canary",
        "thread\x00id",
        "thread\nid",
    ],
)
def test_native_identifier_rejects_unbounded_credential_or_path_material(value: object) -> None:
    with pytest.raises(RuntimeError, match="invalid identifier") as excinfo:
        validated_native_identifier(value, label="test provider")

    assert "native-id-secret-canary" not in str(excinfo.value)


def test_native_stream_url_encodes_valid_colon_segments() -> None:
    assert native_run_stream_url("tenant:thread", "run:1") == (
        "/threads/tenant%3Athread/runs/run%3A1/stream?stream_mode=custom"
    )


def test_native_identifier_rejects_string_subclass_without_hooks() -> None:
    class HostileIdentifier(str):
        called = False

        def __len__(self) -> int:
            self.called = True
            raise AssertionError("native identifier hooks must not execute")

    value = HostileIdentifier("run-1")
    with pytest.raises(RuntimeError, match="invalid identifier"):
        validated_native_identifier(value, label="native search")
    assert value.called is False


@pytest.mark.parametrize(
    "page",
    [
        {"not": "a list"},
        [{"id": "one"}, {"id": "two"}],
        [{"id": "one"}, "not-an-object"],
    ],
)
def test_native_mapping_page_rejects_wrong_shape_or_count(page: object) -> None:
    with pytest.raises(RuntimeError, match="invalid or oversized page"):
        validated_native_mapping_page(page, requested_limit=1, label="native search")


def test_native_mapping_page_rejects_container_subclasses_without_hooks() -> None:
    class HostileList(list[object]):
        called = False

        def __len__(self) -> int:
            self.called = True
            raise AssertionError("native page hooks must not execute")

    class HostileDict(dict[str, object]):
        called = False

        def items(self):  # type: ignore[no-untyped-def]
            self.called = True
            raise AssertionError("native row hooks must not execute")

    hostile_page = HostileList([{"id": "one"}])
    hostile_row = HostileDict(id="one")

    with pytest.raises(RuntimeError, match="invalid or oversized page"):
        validated_native_mapping_page(
            hostile_page,
            requested_limit=1,
            label="native search",
        )
    with pytest.raises(RuntimeError, match="invalid or oversized page"):
        validated_native_mapping_page(
            [hostile_row],
            requested_limit=1,
            label="native search",
        )

    assert hostile_page.called is False
    assert hostile_row.called is False


def test_native_mapping_page_rejects_integer_subclasses_without_hooks() -> None:
    hooks: list[str] = []

    class HostileInt(int):
        def __lt__(self, other: object) -> bool:
            del other
            hooks.append("lt")
            raise AssertionError("integer hook executed")

        def bit_length(self) -> int:
            hooks.append("bit-length")
            raise AssertionError("integer hook executed")

    with pytest.raises(RuntimeError, match="invalid or oversized page"):
        validated_native_mapping_page([], requested_limit=HostileInt(1), label="native search")
    with pytest.raises(RuntimeError, match="invalid or oversized page"):
        validated_native_mapping_page(
            [{"value": HostileInt(1)}],
            requested_limit=1,
            label="native search",
        )

    assert hooks == []


def test_native_mapping_page_rejects_single_row_byte_amplification_before_serializing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_serializer(*args: object, **kwargs: object) -> None:
        raise AssertionError("oversized text must fail before JSON serialization")

    monkeypatch.setattr(public_projection, "validate_json_object", unexpected_serializer)
    with pytest.raises(RuntimeError, match="invalid or oversized page"):
        validated_native_mapping_page(
            [{"metadata": {"payload": "x" * 1_024}}],
            requested_limit=1,
            label="native search",
            max_bytes=512,
        )


def test_native_mapping_page_counts_json_escape_amplification_before_serializing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_serializer(*args: object, **kwargs: object) -> None:
        raise AssertionError("escaped text must fail before JSON serialization")

    monkeypatch.setattr(public_projection, "validate_json_object", unexpected_serializer)
    with pytest.raises(RuntimeError, match="invalid or oversized page"):
        validated_native_mapping_page(
            [{"metadata": {"payload": '"\\\x01' * 100}}],
            requested_limit=1,
            label="native search",
            max_bytes=512,
        )


def test_native_mapping_page_rejects_non_string_keys_without_type_error() -> None:
    page = [{"metadata": {1: "value"}}]  # type: ignore[dict-item]

    with pytest.raises(RuntimeError, match="invalid or oversized page"):
        validated_native_mapping_page(
            page,
            requested_limit=1,
            label="native search",
        )


def test_native_mapping_page_detaches_secret_bearing_validator_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "native-page-validator-secret-canary"

    def explode(*_args: object, **_kwargs: object) -> None:
        raise ValueError(secret)

    monkeypatch.setattr(public_projection, "validate_json_object", explode)

    with pytest.raises(RuntimeError, match="invalid or oversized page") as exc_info:
        validated_native_mapping_page(
            [{"id": "run-1"}],
            requested_limit=1,
            label="native search",
        )

    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    assert secret not in repr(exc_info.value)


def test_native_mapping_page_rejects_node_or_depth_amplification() -> None:
    with pytest.raises(RuntimeError, match="invalid or oversized page"):
        validated_native_mapping_page(
            [{"metadata": {"items": list(range(100))}}],
            requested_limit=1,
            label="native search",
            max_nodes=32,
        )
    with pytest.raises(RuntimeError, match="invalid or oversized page"):
        validated_native_mapping_page(
            [{"metadata": {"nested": {"too": {"deep": True}}}}],
            requested_limit=1,
            label="native search",
            max_depth=3,
        )


def test_public_engine_handle_rejects_hostile_mapping_and_string_without_hooks() -> None:
    hooks: list[str] = []

    class HostileDict(dict[str, object]):
        def get(self, key: str, default: object = None) -> object:
            del key, default
            hooks.append("get")
            raise AssertionError("mapping hook executed")

    class HostileString(str):
        def __len__(self) -> int:
            hooks.append("len")
            raise AssertionError("string hook executed")

    assert public_engine_handle_summary(HostileDict(engine="sim")) is None
    assert public_engine_handle_summary({"engine": HostileString("sim")}) is None
    assert hooks == []


def test_public_test_result_rejects_hostile_mapping_and_primitives_without_hooks() -> None:
    hooks: list[str] = []

    class HostileDict(dict[str, object]):
        def items(self):  # type: ignore[no-untyped-def]
            hooks.append("items")
            raise AssertionError("mapping hook executed")

    class HostileString(str):
        def strip(self, *args: object, **kwargs: object) -> str:
            del args, kwargs
            hooks.append("strip")
            raise AssertionError("string hook executed")

    assert public_test_result_summary(HostileDict()) is None
    assert (
        public_test_result_summary(
            {
                "engine": HostileString("sim"),
                "passed": True,
                "kpis": {},
                "sla_breaches": [],
                "notes": None,
            }
        )
        is None
    )
    assert hooks == []
