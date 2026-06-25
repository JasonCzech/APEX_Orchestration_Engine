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
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from apex.bootstrap.runner import BootstrapError, apply_document
from apex.bootstrap.schema import BootstrapDocument


def _load_document(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    suffix = "" if path == "-" else Path(path).suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # transitive (langchain-core); JSON needs no dependency
        except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
            raise BootstrapError(
                "YAML bootstrap files need PyYAML installed; use JSON or `pip install pyyaml`"
            ) from exc
        data = yaml.safe_load(raw)
    else:
        data = json.loads(raw)
    if not isinstance(data, dict):
        raise BootstrapError(f"bootstrap document must be a mapping, got {type(data).__name__}")
    return data


async def _run(doc: BootstrapDocument) -> None:
    from apex.persistence.db import get_sessionmaker

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

    try:
        doc = BootstrapDocument.model_validate(_load_document(args.file))
    except (BootstrapError, ValueError) as exc:
        print(f"apex.bootstrap: invalid document: {exc}", file=sys.stderr)
        return 2

    try:
        asyncio.run(_run(doc))
    except BootstrapError as exc:
        print(f"apex.bootstrap: {exc}", file=sys.stderr)
        return 1
    except (SQLAlchemyError, OSError) as exc:
        message = f"apex.bootstrap: database unavailable ({exc.__class__.__name__})"
        if args.graceful:
            print(f"{message}; --graceful set, treating as no-op", file=sys.stderr)
            return 0
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
