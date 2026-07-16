import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
import structlog

from apex.app.logging import _redact_event_dict, configure_logging


def test_logging_redacts_secret_fields_recursively() -> None:
    redacted = _redact_event_dict(
        None,
        "info",
        {
            "event": "adapter.request",
            "api_key": "sk-test",
            "nested": {"password": "pw", "safe": "ok"},
            "variants": {
                "privateKey": "private-key-material",
                "connectionString": "opaque-connection-secret",
                "database_uri": "postgresql://user:password@example.test/db",
                "dsn": "opaque-dsn-secret",
            },
            "items": [{"authorization": "Bearer token"}],
            "headers": [
                ("authorization", "Bearer tuple-token"),
                ("x-apex-trusted-loopback", "process-capability"),
            ],
            "cookies": [("cookie", "session=dashboard-session")],
            "urls": {
                "https://user:pass@example.com/path?token=abc&safe=1",
                "https://example.com/path?api_key=abc",
                (
                    "https://objects.example.com/result?X-Amz-Credential=AKIA%2Fscope"
                    "&X-Amz-Signature=deadbeef&safe=1"
                ),
                "https://app.example.com/callback#access_token=oauth-token&state=safe",
            },
        },
    )

    assert redacted["api_key"] == "[redacted]"
    assert redacted["nested"] == {"password": "[redacted]", "safe": "ok"}
    assert redacted["variants"] == {
        "privateKey": "[redacted]",
        "connectionString": "[redacted]",
        "database_uri": "[redacted]",
        "dsn": "[redacted]",
    }
    assert redacted["items"] == [{"authorization": "[redacted]"}]
    assert redacted["headers"] == [
        ("authorization", "[redacted]"),
        ("x-apex-trusted-loopback", "[redacted]"),
    ]
    assert redacted["cookies"] == "[redacted]"
    assert redacted["urls"] == {
        "https://[REDACTED]@example.com/path?token=[REDACTED]&safe=1",
        "https://example.com/path?api_key=[REDACTED]",
        (
            "https://objects.example.com/result?X-Amz-Credential=[REDACTED]"
            "&X-Amz-Signature=[REDACTED]&safe=1"
        ),
        "https://app.example.com/callback#access_token=[REDACTED]&state=safe",
    }


def test_logging_redaction_bounds_recursive_and_oversized_values() -> None:
    recursive: dict[str, object] = {}
    recursive["child"] = recursive
    oversized = "x" * 10_000 + "?token=must-not-survive"

    redacted = _redact_event_dict(
        None,
        "warning",
        {
            "recursive": recursive,
            "oversized": oversized,
            "error": RuntimeError("request failed: Basic dXNlcjpwYXNz"),
            "binary": b"Bearer binary-secret",
        },
    )

    assert redacted["recursive"] == {"child": "[truncated]"}
    assert redacted["oversized"] == "x" * 4_096
    assert redacted["error"] == "request failed: Basic [REDACTED]"
    assert redacted["binary"] == "Bearer [REDACTED]"


def test_logging_redacts_credentials_embedded_in_generic_string_fields() -> None:
    redacted = _redact_event_dict(
        None,
        "error",
        {
            "reason": "upstream rejected password=plain-text; retry disabled",
            "error": RuntimeError('{"client_secret":"json-secret","safe":"ok"}'),
            "detail": (
                "signed URL https://objects.test/result?X-Amz-Credential=AKIA%2Fscope"
                "&X-Amz-Signature=deadbeef&safe=1"
            ),
            "message": "digest abcdef and api-token='token-value'\x00tail",
            "camel": "stripeApiKey=camel-secret; retry disabled",
        },
    )

    assert redacted["reason"] == "upstream rejected password=[REDACTED]; retry disabled"
    assert redacted["error"] == '{"client_secret":"[REDACTED]","safe":"ok"}'
    assert redacted["detail"] == (
        "signed URL https://objects.test/result?X-Amz-Credential=[REDACTED]"
        "&X-Amz-Signature=[REDACTED]&safe=1"
    )
    assert redacted["message"] == "digest [REDACTED] and api-token='[REDACTED]'\\0tail"
    assert redacted["camel"] == "stripeApiKey=[REDACTED]; retry disabled"


def test_logging_never_stringifies_arbitrary_scalar_objects() -> None:
    class CredentialBearingObject:
        class_called = False
        str_called = False
        repr_called = False

        def __getattribute__(self, name: str) -> Any:
            if name == "__class__":
                type(self).class_called = True
                raise AssertionError("scalar __class__ descriptor must not be called")
            return object.__getattribute__(self, name)

        def __str__(self) -> str:
            self.str_called = True
            return "provider response password=object-secret"

        def __repr__(self) -> str:
            self.repr_called = True
            return "<CredentialBearingObject password=repr-secret>"

    payload = CredentialBearingObject()
    redacted = _redact_event_dict(
        None,
        "error",
        {"payload": payload, "attempt": 2, "retrying": False},
    )

    assert redacted == {
        "payload": "[unsupported value]",
        "attempt": 2,
        "retrying": False,
    }
    assert payload.str_called is False
    assert payload.repr_called is False
    assert payload.class_called is False
    rendered = str(structlog.processors.JSONRenderer()(None, "error", redacted))
    assert "object-secret" not in rendered
    assert "repr-secret" not in rendered


def test_logging_replaces_oversized_and_nonfinite_exact_numeric_scalars() -> None:
    huge = 1 << 1_000_000

    redacted = _redact_event_dict(
        None,
        "error",
        cast(Any, {"huge": huge, huge: "unsafe-key", "infinite": float("inf")}),
    )

    assert redacted == {
        "huge": "[unsupported value]",
        "[unsupported credential key]": "[redacted]",
        "infinite": "[unsupported value]",
    }


def test_logging_scrubs_mapping_and_tuple_keys_before_rendering() -> None:
    class CredentialBearingKey:
        str_called = False
        repr_called = False

        def __str__(self) -> str:
            self.str_called = True
            return "authorization=Bearer object-key-secret"

        def __repr__(self) -> str:
            self.repr_called = True
            return "<CredentialBearingKey password=repr-key-secret>"

    dynamic_key = CredentialBearingKey()
    redacted = _redact_event_dict(
        None,
        "error",
        {
            "payload": {
                "password=nested-key-secret": "also-sensitive-because-key-is-secret-like",
                dynamic_key: "header-value",
            },
            "headers": [(dynamic_key, "tuple-value")],
        },
    )

    assert redacted == {
        "payload": {
            "password=[REDACTED]": "[redacted]",
            "[unsupported credential key]": "[redacted]",
        },
        "headers": [("[unsupported credential key]", "[redacted]")],
    }
    assert dynamic_key.str_called is False
    assert dynamic_key.repr_called is False
    rendered = str(structlog.processors.JSONRenderer()(None, "error", redacted))
    assert "nested-key-secret" not in rendered
    assert "object-key-secret" not in rendered
    assert "repr-key-secret" not in rendered
    assert "tuple-value" not in rendered


def test_logging_never_invokes_hostile_exception_string_hooks() -> None:
    class HostileException(RuntimeError):
        class_called = False
        str_called = False
        repr_called = False

        def __getattribute__(self, name: str) -> Any:
            if name == "__class__":
                type(self).class_called = True
                raise AssertionError("exception __class__ descriptor must not be called")
            return BaseException.__getattribute__(self, name)

        def __str__(self) -> str:
            self.str_called = True
            raise AssertionError("exception __str__ must not be called")

        def __repr__(self) -> str:
            self.repr_called = True
            raise AssertionError("exception __repr__ must not be called")

    error = HostileException(object())

    redacted = _redact_event_dict(None, "error", {"error": error})

    assert redacted == {"error": "[unsupported value]"}
    assert error.str_called is False
    assert error.repr_called is False
    assert error.class_called is False


def test_logging_never_traverses_hostile_container_subclasses() -> None:
    class HostileMapping(dict[str, Any]):
        called = False

        def __getattribute__(self, name: str) -> Any:
            if name == "__class__":
                type(self).called = True
                raise AssertionError("mapping __class__ descriptor must not be called")
            return dict.__getattribute__(self, name)

        def items(self) -> Any:
            self.called = True
            raise AssertionError("mapping items must not be called")

        def __iter__(self) -> Iterator[str]:
            self.called = True
            raise AssertionError("mapping iteration must not be called")

        def __len__(self) -> int:
            self.called = True
            raise AssertionError("mapping length must not be called")

        def __str__(self) -> str:
            self.called = True
            raise AssertionError("mapping stringification must not be called")

    class HostileList(list[Any]):
        called = False

        def __iter__(self) -> Iterator[Any]:
            self.called = True
            raise AssertionError("list iteration must not be called")

        def __len__(self) -> int:
            self.called = True
            raise AssertionError("list length must not be called")

        def __getitem__(self, index: Any) -> Any:
            self.called = True
            raise AssertionError("list indexing must not be called")

        def __repr__(self) -> str:
            self.called = True
            raise AssertionError("list repr must not be called")

    hostile_mapping = HostileMapping(password="must-not-be-read")
    hostile_list = HostileList(["Bearer must-not-be-read"])

    redacted = _redact_event_dict(
        None,
        "error",
        {"mapping": hostile_mapping, "sequence": hostile_list},
    )

    assert redacted == {
        "mapping": "[unsupported container]",
        "sequence": "[unsupported container]",
    }
    assert hostile_mapping.called is False
    assert hostile_list.called is False


def test_logging_rejects_a_hostile_top_level_event_mapping_without_traversal() -> None:
    class HostileEventDict(dict[str, Any]):
        called = False

        def items(self) -> Any:
            self.called = True
            raise AssertionError("event items must not be called")

    event = HostileEventDict(event="unsafe")

    assert _redact_event_dict(None, "error", event) == {"event": "[unsupported container]"}
    assert event.called is False


@pytest.mark.parametrize(
    "locked_down,renderer_cls,min_level",
    [
        (True, structlog.processors.JSONRenderer, logging.INFO),
        (False, structlog.dev.ConsoleRenderer, logging.DEBUG),
    ],
)
def test_configure_logging_is_environment_aware(
    monkeypatch: pytest.MonkeyPatch, locked_down: bool, renderer_cls: type, min_level: int
) -> None:
    monkeypatch.setattr(
        "apex.app.logging.get_settings", lambda: SimpleNamespace(is_locked_down=locked_down)
    )
    try:
        configure_logging()
        config = structlog.get_config()
        assert isinstance(config["processors"][-1], renderer_cls)
        # make_filtering_bound_logger caches one class per level; identity proves the floor.
        assert config["wrapper_class"] is structlog.make_filtering_bound_logger(min_level)
    finally:
        structlog.reset_defaults()
