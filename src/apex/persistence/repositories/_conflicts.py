"""Bounded driver diagnostics used only for conservative conflict classification."""

from typing import Any

_MAX_DRIVER_DIAGNOSTIC_CHARS = 4_096
_MAX_CONSTRAINT_NAME_CHARS = 255


def driver_constraint_name(original: Any) -> str | None:
    """Read a small exact PostgreSQL constraint label without stringifying errors."""

    if not issubclass(type(original), BaseException):
        return None
    try:
        diag = object.__getattribute__(original, "diag")
        value = object.__getattribute__(diag, "constraint_name")
    except BaseException:
        return None
    if type(value) is str and 1 <= len(value) <= _MAX_CONSTRAINT_NAME_CHARS and "\x00" not in value:
        return value
    return None


def bounded_driver_message(original: Any) -> str:
    """Extract only bounded exact builtin exception arguments.

    SQLite exposes unique-column diagnostics through ``Exception.args``. Reading
    the base descriptor bypasses an exception subclass' ``__str__`` and any
    overridden ``args`` property; non-text arguments are ignored conservatively.
    """

    if not issubclass(type(original), BaseException):
        return ""
    try:
        args_descriptor = BaseException.__dict__["args"]
        args = args_descriptor.__get__(original, type(original))
    except BaseException:
        return ""
    if type(args) is not tuple:
        return ""

    parts: list[str] = []
    remaining = _MAX_DRIVER_DIAGNOSTIC_CHARS
    for value in args[:8]:
        if remaining <= 0:
            break
        if type(value) is str:
            part = value[:remaining]
        elif type(value) is bytes:
            part = value[:remaining].decode("utf-8", errors="ignore")
        else:
            continue
        parts.append(part)
        remaining -= len(part)
    return " ".join(parts).lower()
