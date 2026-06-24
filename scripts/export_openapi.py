"""Export the committed /v1 OpenAPI contract to docs/api/apex-v1.openapi.json.

The committed spec is the source of truth for generated SDKs (packages/api-client)
and the CI drift gate: regenerating it must be byte-for-byte identical to the file
in git, otherwise the API changed without the contract being re-exported.

Determinism: keys are sorted and output is plain ASCII JSON with a trailing
newline. Every route MUST declare an explicit camelCase ``operation_id`` (and they
must be unique) — this script exits 2 with a message when the convention is
violated, so accidental auto-generated operation ids never reach the contract.

Usage: uv run python scripts/export_openapi.py  (or ``make openapi``)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from fastapi.routing import APIRoute

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = REPO_ROOT / "src"
SPEC_PATH = REPO_ROOT / "docs" / "api" / "apex-v1.openapi.json"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


def validate_operation_ids(app: Any) -> list[str]:
    """Return violation messages for routes lacking explicit/unique operation ids."""
    problems: list[str] = []
    seen: dict[str, str] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        where = f"{sorted(route.methods - {'HEAD'})} {route.path}"
        if not route.operation_id:
            problems.append(f"missing explicit operation_id: {where}")
            continue
        if route.operation_id in seen:
            problems.append(
                f"duplicate operation_id {route.operation_id!r}: {where} "
                f"collides with {seen[route.operation_id]}"
            )
        else:
            seen[route.operation_id] = where
    return problems


def build_spec(app: Any) -> dict[str, Any]:
    """Build the OpenAPI document, validating contract conventions first."""
    problems = validate_operation_ids(app)
    if problems:
        for problem in problems:
            print(f"export_openapi: {problem}", file=sys.stderr)
        sys.exit(2)
    return app.openapi()


def render_spec(spec: dict[str, Any]) -> str:
    """Serialize deterministically: sorted keys, ASCII, trailing newline."""
    return json.dumps(spec, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def main() -> None:
    from apex.app.http import app

    spec = build_spec(app)
    SPEC_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPEC_PATH.write_text(render_spec(spec), encoding="utf-8")
    paths = spec.get("paths", {})
    operations = sum(len(ops) for ops in paths.values())
    print(
        f"export_openapi: wrote {SPEC_PATH.relative_to(REPO_ROOT)} "
        f"({len(paths)} paths, {operations} operations)"
    )


if __name__ == "__main__":
    main()
