"""CLI entrypoint: `python -m apex.bootstrap [FILE]` (the Helm bootstrap Job runs this).

Loads a JSON or YAML bootstrap document, validates it, and applies it in one
transaction. Strict by default (a database/validation error exits non-zero so the
Helm hook fails and the rollout is blocked); pass --graceful for the dev-script
behavior of treating an unreachable database as a no-op.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from apex.bootstrap.runner import BootstrapError, apply_document
from apex.bootstrap.schema import BootstrapDocument
from apex.domain.diagnostics import safe_type_name
from apex.settings import database_uri_has_safe_transport, get_settings

MAX_BOOTSTRAP_BYTES = 1_048_576
MAX_BOOTSTRAP_DEPTH = 32
MAX_BOOTSTRAP_NODES = 10_000


def _read_document(path: str) -> str:
    read_error_kind: str | None = None
    value = ""
    encoded_size = 0
    try:
        if path == "-":
            value = sys.stdin.read(MAX_BOOTSTRAP_BYTES + 1)
            encoded_size = len(value.encode("utf-8"))
        else:
            with Path(path).open("rb") as stream:
                payload = stream.read(MAX_BOOTSTRAP_BYTES + 1)
            encoded_size = len(payload)
            value = payload.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        read_error_kind = safe_type_name(exc)
    if read_error_kind is not None:
        raise BootstrapError(f"cannot read bootstrap document ({read_error_kind})")
    if encoded_size > MAX_BOOTSTRAP_BYTES:
        raise BootstrapError(f"bootstrap document exceeds the {MAX_BOOTSTRAP_BYTES}-byte limit")
    return value


def _validate_json_nesting(raw: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for char in raw:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > MAX_BOOTSTRAP_DEPTH:
                raise BootstrapError(
                    f"bootstrap document exceeds maximum depth {MAX_BOOTSTRAP_DEPTH}"
                )
        elif char in "]}":
            depth -= 1


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BootstrapError("bootstrap document contains a duplicate key")
        result[key] = value
    return result


def _validate_document_tree(data: Any) -> None:
    nodes = 0
    seen_containers: set[int] = set()
    stack: list[tuple[Any, int]] = [(data, 1)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_BOOTSTRAP_NODES:
            raise BootstrapError(f"bootstrap document exceeds the {MAX_BOOTSTRAP_NODES}-node limit")
        if depth > MAX_BOOTSTRAP_DEPTH:
            raise BootstrapError(f"bootstrap document exceeds maximum depth {MAX_BOOTSTRAP_DEPTH}")
        if isinstance(value, Mapping):
            identity = id(value)
            if identity in seen_containers:
                raise BootstrapError("bootstrap document must not contain aliases or cycles")
            seen_containers.add(identity)
            for key, child in value.items():
                if not isinstance(key, str):
                    raise BootstrapError("bootstrap document mapping keys must be strings")
                stack.append((child, depth + 1))
        elif isinstance(value, list):
            identity = id(value)
            if identity in seen_containers:
                raise BootstrapError("bootstrap document must not contain aliases or cycles")
            seen_containers.add(identity)
            stack.extend((child, depth + 1) for child in value)
        elif not isinstance(value, (str, int, bool)) and value is not None:
            if not isinstance(value, float) or not math.isfinite(value):
                raise BootstrapError(
                    "bootstrap document values must use finite JSON-compatible scalar types"
                )


def _load_document(path: str) -> dict[str, Any]:
    raw = _read_document(path)
    suffix = "" if path == "-" else Path(path).suffix.lower()
    data: Any = None
    if suffix in (".yaml", ".yml"):
        yaml_module: Any | None = None
        yaml_error_kind: str | None = None
        try:
            import yaml as yaml_module  # transitive; JSON needs no dependency
        except ModuleNotFoundError:  # pragma: no cover - environment dependent
            pass
        if yaml_module is None:
            raise BootstrapError(
                "YAML bootstrap files need PyYAML installed; use JSON or `pip install pyyaml`"
            )
        yaml = yaml_module
        try:
            depth = 0
            nodes = 0
            for event in yaml.parse(raw, Loader=yaml.SafeLoader):
                if isinstance(event, yaml.events.AliasEvent):
                    raise BootstrapError("bootstrap YAML aliases are not allowed")
                if isinstance(
                    event,
                    (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent),
                ):
                    depth += 1
                    nodes += 1
                    if depth > MAX_BOOTSTRAP_DEPTH:
                        raise BootstrapError(
                            f"bootstrap document exceeds maximum depth {MAX_BOOTSTRAP_DEPTH}"
                        )
                elif isinstance(
                    event,
                    (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent),
                ):
                    depth -= 1
                elif isinstance(event, yaml.events.ScalarEvent):
                    nodes += 1
                if nodes > MAX_BOOTSTRAP_NODES:
                    raise BootstrapError(
                        f"bootstrap document exceeds the {MAX_BOOTSTRAP_NODES}-node limit"
                    )

            class UniqueKeyLoader(yaml.SafeLoader):
                pass

            def construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict[Any, Any]:
                loader.flatten_mapping(node)
                result: dict[Any, Any] = {}
                for key_node, value_node in node.value:
                    key = loader.construct_object(key_node, deep=deep)
                    invalid_key = False
                    try:
                        duplicate = key in result
                    except TypeError:
                        invalid_key = True
                        duplicate = False
                    if invalid_key:
                        raise BootstrapError("bootstrap YAML mapping keys must be scalar")
                    if duplicate:
                        raise BootstrapError("bootstrap document contains a duplicate key")
                    result[key] = loader.construct_object(value_node, deep=deep)
                return result

            UniqueKeyLoader.add_constructor(
                yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                construct_mapping,
            )
            data = yaml.load(raw, Loader=UniqueKeyLoader)
        except BootstrapError:
            raise
        except (yaml.YAMLError, RecursionError) as exc:
            yaml_error_kind = safe_type_name(exc)
        if yaml_error_kind is not None:
            raise BootstrapError(f"invalid YAML bootstrap document ({yaml_error_kind})")
    else:
        _validate_json_nesting(raw)
        json_error_kind: str | None = None
        try:
            data = json.loads(
                raw,
                object_pairs_hook=_unique_json_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    BootstrapError("non-finite JSON numbers are not allowed")
                ),
            )
        except BootstrapError:
            raise
        except (json.JSONDecodeError, RecursionError) as exc:
            json_error_kind = safe_type_name(exc)
            data = None
        if json_error_kind is not None:
            raise BootstrapError(f"invalid JSON bootstrap document ({json_error_kind})")
    _validate_document_tree(data)
    if not isinstance(data, dict):
        raise BootstrapError(f"bootstrap document must be a mapping, got {safe_type_name(data)}")
    return data


async def _run(doc: BootstrapDocument) -> None:
    from apex.persistence.db import get_sessionmaker

    database = get_settings().database
    if not database_uri_has_safe_transport(database.uri, database.ssl_mode):
        raise BootstrapError("database transport must authenticate every remote server")
    async with get_sessionmaker()() as session:
        report = await apply_document(doc, session, env=os.environ)
        await session.commit()
    print(f"apex.bootstrap: done — {report.summary()}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="apex.bootstrap", description=__doc__)
    parser.add_argument(
        "file",
        nargs="?",
        default=os.environ.get("APEX_BOOTSTRAP_FILE"),
        help="Path to the JSON/YAML bootstrap document ('-' for stdin). "
        "Defaults to $APEX_BOOTSTRAP_FILE.",
    )
    parser.add_argument(
        "--graceful",
        action="store_true",
        help="Treat an unreachable database as a no-op (exit 0). Off by default so the "
        "Helm hook fails loudly on a real error.",
    )
    args = parser.parse_args(argv)
    if not args.file:
        parser.error("no bootstrap file given (pass a path or set $APEX_BOOTSTRAP_FILE)")
    if args.graceful:
        # A production hook must never report success after skipping every
        # bootstrap write. Validate this before reading the document or opening
        # a database connection, and keep configuration details out of stderr.
        try:
            graceful_allowed = not get_settings().is_locked_down
        except Exception:
            graceful_allowed = False
        if not graceful_allowed:
            print(
                "apex.bootstrap: --graceful is allowed only in local/test environments",
                file=sys.stderr,
            )
            return 2

    try:
        doc = BootstrapDocument.model_validate(_load_document(args.file))
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        kinds = sorted({str(error.get("type", "invalid")) for error in errors})[:8]
        summary = ", ".join(kinds) or "invalid"
        print(
            f"apex.bootstrap: invalid document: schema validation failed "
            f"({len(errors)} error(s): {summary})",
            file=sys.stderr,
        )
        return 2
    except BootstrapError as exc:
        print(f"apex.bootstrap: invalid document: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(
            f"apex.bootstrap: invalid document ({safe_type_name(exc)})",
            file=sys.stderr,
        )
        return 2

    try:
        asyncio.run(_run(doc))
    except BootstrapError as exc:
        print(f"apex.bootstrap: {exc}", file=sys.stderr)
        return 1
    except (SQLAlchemyError, OSError) as exc:
        message = f"apex.bootstrap: database unavailable ({safe_type_name(exc)})"
        if args.graceful:
            print(f"{message}; --graceful set, treating as no-op", file=sys.stderr)
            return 0
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
