"""Bounded durable-evidence scalar regressions."""

import pytest

from apex.domain.durable_evidence import sanitize_durable_text


def test_durable_text_bounds_before_nul_replacement() -> None:
    value = "x" * 100_000 + "\x00password=outside-window-secret"

    sanitized = sanitize_durable_text(value, 64)

    assert sanitized is not None
    assert len(sanitized) == 64
    assert "outside-window-secret" not in sanitized


def test_durable_text_does_not_invoke_str_subclass_replace() -> None:
    class HostileText(str):
        def replace(self, *_args: object, **_kwargs: object) -> str:
            raise RuntimeError("hostile-text-secret-canary")

    sanitized = sanitize_durable_text(HostileText("x" * 100_000), 128)

    assert sanitized == "[unsupported-text:HostileText]"


def test_durable_text_does_not_invoke_hostile_metaclass_name_descriptor() -> None:
    class HostileMeta(type):
        called = False

        @property
        def __name__(cls) -> str:  # type: ignore[override]
            cls.called = True
            return "must-not-be-read"

    class HostileText(str, metaclass=HostileMeta):
        pass

    sanitized = sanitize_durable_text(HostileText("safe"), 128)

    assert sanitized == "[unsupported-text:unknown]"
    assert HostileMeta.called is False


@pytest.mark.parametrize("limit", range(1, 14))
def test_durable_text_tiny_limits_remain_exact_and_redacted(limit: int) -> None:
    secret = "tiny-limit-secret-canary"

    sanitized = sanitize_durable_text(
        f"password={secret}\x00" + ("x" * 100),
        limit,
    )

    assert sanitized is not None
    assert len(sanitized) == limit
    assert secret not in sanitized
    assert "\x00" not in sanitized
