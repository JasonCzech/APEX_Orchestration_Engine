from apex.app.logging import _redact_event_dict


def test_logging_redacts_secret_fields_recursively() -> None:
    redacted = _redact_event_dict(
        None,
        "info",
        {
            "event": "adapter.request",
            "api_key": "sk-test",
            "nested": {"password": "pw", "safe": "ok"},
            "items": [{"authorization": "Bearer token"}],
        },
    )

    assert redacted["api_key"] == "[redacted]"
    assert redacted["nested"] == {"password": "[redacted]", "safe": "ok"}
    assert redacted["items"] == [{"authorization": "[redacted]"}]
