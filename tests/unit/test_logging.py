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
            "headers": [("authorization", "Bearer tuple-token")],
            "urls": {
                "https://user:pass@example.com/path?token=abc&safe=1",
                "https://example.com/path?api_key=abc",
            },
        },
    )

    assert redacted["api_key"] == "[redacted]"
    assert redacted["nested"] == {"password": "[redacted]", "safe": "ok"}
    assert redacted["items"] == [{"authorization": "[redacted]"}]
    assert redacted["headers"] == [("authorization", "[redacted]")]
    assert redacted["urls"] == {
        "https://[redacted]@example.com/path?token=[redacted]&safe=1",
        "https://example.com/path?api_key=[redacted]",
    }
