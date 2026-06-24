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
