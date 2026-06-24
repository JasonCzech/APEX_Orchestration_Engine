"""Small option-parsing helpers shared by adapters."""

from __future__ import annotations

from typing import Any

_TRUE_STRINGS = {"true", "1", "yes", "on", "y"}
_FALSE_STRINGS = {"false", "0", "no", "off", "n", ""}


def coerce_bool(value: Any, *, default: bool) -> bool:
    """Interpret a connection-option value as a bool.

    JSON booleans and numbers pass through; strings are parsed case-insensitively so
    a ``"false"`` / ``"0"`` / ``"no"`` option is honored as ``False`` instead of
    ``bool("false")`` silently evaluating to ``True``. Unrecognized values fall back
    to ``default``.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
    return default
