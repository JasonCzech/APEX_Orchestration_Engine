"""Contract gate for the committed /v1 OpenAPI spec.

Four invariants, enforced as plain unit tests so any `pytest` run catches drift:

1. every schema-visible route declares an explicit operation_id (contract stability);
2. all operation ids are unique (SDK generators key methods on them);
3. regenerating the spec matches docs/api/apex-v1.openapi.json byte-for-byte —
   if this fails, run `make openapi` and commit the result.
4. every arbitrary string path/query parameter is length-bounded and rejects
   U+0000 before repository/provider I/O.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.routing import APIRoute

from apex.app.http import app

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "docs" / "api" / "apex-v1.openapi.json"
NO_NUL_PATTERN = r"^[^\x00]*$"


@pytest.fixture(scope="module")
def export_openapi() -> ModuleType:
    """Load scripts/export_openapi.py as a module (scripts/ is not a package)."""
    script = REPO_ROOT / "scripts" / "export_openapi.py"
    spec = importlib.util.spec_from_file_location("export_openapi", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_openapi"] = module
    spec.loader.exec_module(module)
    return module


def _api_routes() -> list[APIRoute]:
    return [r for r in app.routes if isinstance(r, APIRoute) and r.include_in_schema]


def test_every_route_has_explicit_operation_id() -> None:
    missing = [
        f"{sorted(route.methods - {'HEAD'})} {route.path}"
        for route in _api_routes()
        if not route.operation_id
    ]
    assert not missing, f"routes without explicit operation_id: {missing}"


def test_operation_ids_are_unique() -> None:
    ids = [route.operation_id for route in _api_routes() if route.operation_id]
    duplicates = sorted({op for op in ids if ids.count(op) > 1})
    assert not duplicates, f"duplicate operation_ids: {duplicates}"


def _string_schemas(schema: dict[str, Any], components: dict[str, Any]) -> list[dict[str, Any]]:
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        name = ref.removeprefix("#/components/schemas/")
        resolved = components.get(name)
        return _string_schemas(resolved, components) if isinstance(resolved, dict) else []

    result = [schema] if schema.get("type") == "string" else []
    for keyword in ("anyOf", "oneOf", "allOf"):
        choices = schema.get(keyword, [])
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict):
                    result.extend(_string_schemas(choice, components))
    items = schema.get("items")
    if isinstance(items, dict):
        result.extend(_string_schemas(items, components))
    return result


def test_custom_string_route_parameters_are_bounded_and_reject_nul() -> None:
    spec = app.openapi()
    components = spec.get("components", {}).get("schemas", {})
    offenders: list[str] = []
    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method == "parameters" or not isinstance(operation, dict):
                continue
            for parameter in operation.get("parameters", []):
                if parameter.get("in") not in {"path", "query"}:
                    continue
                schema = parameter.get("schema", {})
                for string_schema in _string_schemas(schema, components):
                    enum = string_schema.get("enum")
                    if isinstance(enum, list):
                        if any(isinstance(value, str) and "\x00" in value for value in enum):
                            offenders.append(
                                f"{method.upper()} {path} {parameter['in']}:{parameter['name']} "
                                "has an enum value containing U+0000"
                            )
                        continue
                    if string_schema.get("format") in {"date", "date-time", "uuid"}:
                        continue
                    missing = []
                    if "maxLength" not in string_schema:
                        missing.append("maxLength")
                    if string_schema.get("pattern") != NO_NUL_PATTERN:
                        missing.append("the shared no-NUL pattern")
                    if missing:
                        offenders.append(
                            f"{method.upper()} {path} {parameter['in']}:{parameter['name']} "
                            f"is missing {', '.join(missing)}"
                        )
    assert not offenders, "unbounded string route parameters: " + "; ".join(offenders)


def test_committed_spec_matches_regenerated(export_openapi: ModuleType) -> None:
    assert SPEC_PATH.is_file(), (
        f"missing committed spec {SPEC_PATH}; run `make openapi` and commit it"
    )
    regenerated = export_openapi.render_spec(export_openapi.build_spec(app))
    committed = SPEC_PATH.read_text(encoding="utf-8")
    assert regenerated == committed, (
        "docs/api/apex-v1.openapi.json is stale: the live FastAPI app produces a "
        "different OpenAPI document. Run `make openapi` and commit the diff."
    )
