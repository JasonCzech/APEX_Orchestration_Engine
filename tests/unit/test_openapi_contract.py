"""Contract gate for the committed /v1 OpenAPI spec.

Three invariants, enforced as plain unit tests so any `pytest` run catches drift:

1. every schema-visible route declares an explicit operation_id (contract stability);
2. all operation ids are unique (SDK generators key methods on them);
3. regenerating the spec matches docs/api/apex-v1.openapi.json byte-for-byte —
   if this fails, run `make openapi` and commit the result.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.routing import APIRoute

from apex.app.http import app

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "docs" / "api" / "apex-v1.openapi.json"


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
