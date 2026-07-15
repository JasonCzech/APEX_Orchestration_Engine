import logging
from types import SimpleNamespace

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


def test_logging_scrubs_arbitrary_objects_before_json_renderer_stringifies_them() -> None:
    class CredentialBearingObject:
        def __str__(self) -> str:
            return "provider response password=object-secret"

        def __repr__(self) -> str:
            return "<CredentialBearingObject password=repr-secret>"

    redacted = _redact_event_dict(
        None,
        "error",
        {"payload": CredentialBearingObject(), "attempt": 2, "retrying": False},
    )

    assert redacted == {
        "payload": "provider response password=[REDACTED]",
        "attempt": 2,
        "retrying": False,
    }
    rendered = str(structlog.processors.JSONRenderer()(None, "error", redacted))
    assert "object-secret" not in rendered
    assert "repr-secret" not in rendered


def test_logging_scrubs_mapping_and_tuple_keys_before_rendering() -> None:
    class CredentialBearingKey:
        def __str__(self) -> str:
            return "authorization=Bearer object-key-secret"

        def __repr__(self) -> str:
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
            "authorization=[REDACTED]": "[redacted]",
        },
        "headers": [("authorization=[REDACTED]", "[redacted]")],
    }
    rendered = str(structlog.processors.JSONRenderer()(None, "error", redacted))
    assert "nested-key-secret" not in rendered
    assert "object-key-secret" not in rendered
    assert "repr-key-secret" not in rendered
    assert "tuple-value" not in rendered


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
