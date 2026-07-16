"""Validate the generated LangGraph Dockerfile before image build or release."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "langgraph.json"
MAX_RECIPE_BYTES = 2 * 1024 * 1024
EXPECTED_BASE_IMAGE = "langchain/langgraph-api"
EXPECTED_PYTHON_VERSION = "3.12"
EXPECTED_DOCKER_TAG = (
    "0.10.0-py3.12@sha256:812e919f12ad9605c9a2b443c95fcdc7d4f8cfecaf11de02181f04943864f27c"
)
EXPECTED_GRAPHS = {
    "pipeline": "./src/apex/graphs/pipeline/graph.py:graph",
    "playground": "./src/apex/graphs/playground/graph.py:graph",
    "context": "./src/apex/graphs/context/graph.py:graph",
}
EXPECTED_HTTP = {
    "app": "./src/apex/app/http.py:app",
    "disable_meta": True,
    "disable_ui": True,
    "disable_mcp": True,
    "disable_a2a": True,
    "disable_store": True,
    "disable_webhooks": True,
    "disable_event_streaming": True,
    "cors": {
        "allow_origins": ["http://localhost:5173", "http://127.0.0.1:5173"],
        "allow_methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        "allow_headers": [
            "authorization",
            "content-type",
            "idempotency-key",
            "last-event-id",
            "x-api-key",
            "x-request-id",
        ],
        "allow_credentials": True,
        "expose_headers": [
            "content-location",
            "retry-after",
            "x-pagination-next",
            "x-pagination-total",
        ],
        "max_age": 600,
    },
}
EXPECTED_AUTH = {
    "path": "./src/apex/auth/handlers.py:auth",
    "openapi": {"apiKeyAuth": {"type": "apiKey", "in": "header", "name": "x-api-key"}},
}
EXPECTED_ENV = ".env"
EXPECTED_CONFIG_KEYS = frozenset(
    {
        "python_version",
        "_INTERNAL_docker_tag",
        "base_image",
        "dockerfile_lines",
        "source",
        "graphs",
        "http",
        "auth",
        "env",
    }
)
EXPECTED_DOCKERFILE_LINES = (
    "ARG APEX_BUILD_VERSION=0.0.0+local",
    "ENV APEX_VERSION=${APEX_BUILD_VERSION}",
    "LABEL org.opencontainers.image.version=${APEX_BUILD_VERSION}",
)
EXPECTED_ADDS = (
    ("pyproject.toml", "/tmp/uv_export/project/pyproject.toml"),
    ("uv.lock", "/tmp/uv_export/project/uv.lock"),
    ("README.md", "/deps/workspace/README.md"),
    ("langgraph.json", "/deps/workspace/langgraph.json"),
    ("pyproject.toml", "/deps/workspace/pyproject.toml"),
    ("src", "/deps/workspace/src"),
    ("uv.lock", "/deps/workspace/uv.lock"),
)
EXPECTED_UV_EXPORT = (
    "RUN uv export --package apex-orchestration-engine --frozen --no-hashes "
    "--no-emit-project --no-emit-workspace -o uv_requirements.txt"
)
EXPECTED_LOCK_INSTALL = (
    "RUN PYTHONDONTWRITEBYTECODE=1 uv pip install --system --no-cache-dir "
    "-c /api/constraints.txt -r uv_requirements.txt"
)
EXPECTED_PROJECT_INSTALL = (
    "RUN PYTHONDONTWRITEBYTECODE=1 uv pip install --system --no-cache-dir "
    "-c /api/constraints.txt --no-deps -e ."
)
EXPECTED_LANGGRAPH_AUTH = (
    "ENV LANGGRAPH_AUTH='{"
    '"path": "/deps/workspace/src/apex/auth/handlers.py:auth", '
    '"openapi": {"apiKeyAuth": {"type": "apiKey", "in": "header", '
    '"name": "x-api-key"}}}'
    "'"
)
EXPECTED_LANGGRAPH_HTTP = (
    "ENV LANGGRAPH_HTTP='{"
    '"app": "/deps/workspace/src/apex/app/http.py:app", '
    '"disable_meta": true, "disable_ui": true, "disable_mcp": true, '
    '"disable_a2a": true, "disable_store": true, "disable_webhooks": true, '
    '"disable_event_streaming": true, "cors": {'
    '"allow_origins": ["http://localhost:5173", "http://127.0.0.1:5173"], '
    '"allow_methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], '
    '"allow_headers": ["authorization", "content-type", "idempotency-key", '
    '"last-event-id", "x-api-key", "x-request-id"], "allow_credentials": true, '
    '"expose_headers": ["content-location", "retry-after", "x-pagination-next", '
    '"x-pagination-total"], "max_age": 600}}'
    "'"
)
EXPECTED_LANGSERVE_GRAPHS = (
    "ENV LANGSERVE_GRAPHS='{"
    '"pipeline": "/deps/workspace/src/apex/graphs/pipeline/graph.py:graph", '
    '"playground": "/deps/workspace/src/apex/graphs/playground/graph.py:graph", '
    '"context": "/deps/workspace/src/apex/graphs/context/graph.py:graph"}'
    "'"
)
EXPECTED_BASE_INTEGRITY_RUN = (
    "RUN mkdir -p /api/langgraph_api /api/langgraph_runtime /api/langgraph_license "
    "&& touch /api/langgraph_api/__init__.py /api/langgraph_runtime/__init__.py "
    "/api/langgraph_license/__init__.py"
)
EXPECTED_BASE_REINSTALL_RUN = (
    "RUN PYTHONDONTWRITEBYTECODE=1 uv pip install --system --no-cache-dir --no-deps -e /api"
)
EXPECTED_BUILD_TOOL_CLEANUP = (
    "RUN pip uninstall -y pip setuptools wheel",
    (
        "RUN rm -rf /usr/local/lib/python*/site-packages/pip* "
        "/usr/local/lib/python*/site-packages/setuptools* "
        "/usr/local/lib/python*/site-packages/wheel* "
        '&& find /usr/local/bin -name "pip*" -delete || true'
    ),
    (
        "RUN rm -rf /usr/lib/python*/site-packages/pip* "
        "/usr/lib/python*/site-packages/setuptools* "
        "/usr/lib/python*/site-packages/wheel* "
        '&& find /usr/bin -name "pip*" -delete || true'
    ),
    "RUN uv pip uninstall --system pip setuptools wheel && rm /usr/bin/uv /usr/bin/uvx",
)
EXPECTED_COMMENTS = (
    "# -- Installing dependencies from uv.lock --",
    "# -- End of uv.lock dependencies install --",
    "# -- Adding workspace package . --",
    "# -- End of workspace package . --",
    "# -- Ensure user deps didn't inadvertently overwrite langgraph-api",
    "# -- End of ensuring user deps didn't inadvertently overwrite langgraph-api --",
    "# -- Removing build deps from the final image ~<:===~~~ --",
)
_PARSER_DIRECTIVE = re.compile(r"#\s*(?:syntax|escape|check)\s*=", re.IGNORECASE)


class RecipeValidationError(ValueError):
    """The generated image recipe violated the release contract."""


def _read_bounded_text(path: Path, *, label: str) -> str:
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_RECIPE_BYTES + 1)
    except OSError:
        # User-controlled paths and platform diagnostics are not part of the
        # stable validation boundary; retain only the bounded public message.
        raise RecipeValidationError(f"{label} is unavailable") from None
    # Bound the read itself instead of trusting a pre-read stat: a file can be
    # replaced or extended between stat() and read(), including in a shared CI
    # workspace.
    if not payload or len(payload) > MAX_RECIPE_BYTES:
        raise RecipeValidationError(f"{label} size is invalid")
    try:
        return payload.decode("utf-8")
    except UnicodeError:
        raise RecipeValidationError(f"{label} is not readable UTF-8") from None


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, nested in pairs:
        if key in value:
            raise RecipeValidationError("LangGraph config contains a duplicate object key")
        value[key] = nested
    return value


def _load_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            _read_bounded_text(path, label="LangGraph config"),
            object_pairs_hook=_reject_duplicate_object,
        )
    except json.JSONDecodeError:
        # JSONDecodeError retains a document excerpt. The config is reviewed as
        # untrusted input, so never attach that excerpt to the public error.
        raise RecipeValidationError("LangGraph config is not valid JSON") from None
    if not isinstance(value, dict):
        raise RecipeValidationError("LangGraph config must be an object")
    return value


def _expected_base(config: dict[str, Any]) -> str:
    if set(config) != EXPECTED_CONFIG_KEYS:
        raise RecipeValidationError("LangGraph config keys are not the reviewed key set")
    if config.get("python_version") != EXPECTED_PYTHON_VERSION:
        raise RecipeValidationError("LangGraph Python version is not the reviewed version")
    if config.get("base_image") != EXPECTED_BASE_IMAGE:
        raise RecipeValidationError("LangGraph base image is not the reviewed image")
    docker_tag = config.get("_INTERNAL_docker_tag")
    if docker_tag != EXPECTED_DOCKER_TAG:
        raise RecipeValidationError("LangGraph base tag is not the reviewed image digest")
    if config.get("source") != {"kind": "uv", "root": "."} or "dependencies" in config:
        raise RecipeValidationError("LangGraph dependencies must come from the root frozen uv lock")
    if config.get("graphs") != EXPECTED_GRAPHS:
        raise RecipeValidationError("LangGraph graph definitions are not the reviewed definitions")
    if config.get("http") != EXPECTED_HTTP:
        raise RecipeValidationError("LangGraph HTTP config is not the reviewed config")
    if config.get("auth") != EXPECTED_AUTH:
        raise RecipeValidationError("LangGraph auth config is not the reviewed config")
    if config.get("env") != EXPECTED_ENV:
        raise RecipeValidationError("LangGraph env path is not the reviewed path")
    if config.get("dockerfile_lines") != list(EXPECTED_DOCKERFILE_LINES):
        raise RecipeValidationError("LangGraph Dockerfile extension lines are not allowlisted")
    return f"FROM {EXPECTED_BASE_IMAGE}:{docker_tag}"


def _docker_instruction(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    return stripped.split(None, 1)[0].upper()


def _expected_instructions(expected_from: str) -> tuple[str, ...]:
    return (
        expected_from,
        *EXPECTED_DOCKERFILE_LINES,
        f"ADD {EXPECTED_ADDS[0][0]} {EXPECTED_ADDS[0][1]}",
        f"ADD {EXPECTED_ADDS[1][0]} {EXPECTED_ADDS[1][1]}",
        "WORKDIR /tmp/uv_export/project",
        EXPECTED_UV_EXPORT,
        EXPECTED_LOCK_INSTALL,
        "RUN rm -rf /tmp/uv_export",
        *(f"ADD {source} {destination}" for source, destination in EXPECTED_ADDS[2:]),
        "WORKDIR /deps/workspace",
        EXPECTED_PROJECT_INSTALL,
        EXPECTED_LANGGRAPH_AUTH,
        EXPECTED_LANGGRAPH_HTTP,
        EXPECTED_LANGSERVE_GRAPHS,
        EXPECTED_BASE_INTEGRITY_RUN,
        EXPECTED_BASE_REINSTALL_RUN,
        *EXPECTED_BUILD_TOOL_CLEANUP,
        "WORKDIR /deps/workspace",
    )


def validate_langgraph_recipe(config_path: Path, dockerfile_path: Path) -> None:
    """Raise when the generated Dockerfile is mutable or escapes reviewed inputs."""

    expected_from = _expected_base(_load_config(config_path))
    dockerfile = _read_bounded_text(dockerfile_path, label="generated Dockerfile")
    lines = dockerfile.splitlines()

    if any(_PARSER_DIRECTIVE.match(line.lstrip()) for line in lines):
        raise RecipeValidationError("generated Dockerfile contains a parser directive")
    comments = tuple(line.strip() for line in lines if line.strip().startswith("#"))
    if comments != EXPECTED_COMMENTS:
        raise RecipeValidationError("generated Dockerfile comments are not allowlisted")

    from_lines = [line for line in lines if _docker_instruction(line) == "FROM"]
    if from_lines != [expected_from]:
        raise RecipeValidationError("generated Dockerfile does not use the pinned base image")

    if any(lines.count(line) != 1 for line in EXPECTED_DOCKERFILE_LINES):
        raise RecipeValidationError("generated Dockerfile extension lines do not match exactly")

    adds: list[tuple[str, str]] = []
    for line in lines:
        instruction = _docker_instruction(line)
        if instruction == "COPY":
            raise RecipeValidationError("generated Dockerfile contains a non-allowlisted COPY")
        if instruction != "ADD":
            continue
        # LangGraph emits the shell form `ADD <one source> <absolute destination>`.
        # Reject flags, JSON form, multiple sources, URLs, and any newly added path.
        parts = line.strip().split()
        if len(parts) != 3 or parts[0] != "ADD" or not parts[2].startswith("/"):
            raise RecipeValidationError("generated Dockerfile contains an invalid ADD")
        adds.append((parts[1], parts[2]))
    if tuple(adds) != EXPECTED_ADDS:
        raise RecipeValidationError("generated Dockerfile ADD paths are not allowlisted")

    if lines.count(EXPECTED_UV_EXPORT) != 1:
        raise RecipeValidationError("generated Dockerfile does not export the frozen uv lock")
    if lines.count(EXPECTED_LOCK_INSTALL) != 1:
        raise RecipeValidationError("generated Dockerfile does not install the frozen uv export")
    if lines.count(EXPECTED_PROJECT_INSTALL) != 1:
        raise RecipeValidationError(
            "generated Dockerfile does not install the project without deps"
        )

    instructions = tuple(line.strip() for line in lines if _docker_instruction(line) is not None)
    if instructions != _expected_instructions(expected_from):
        raise RecipeValidationError("generated Dockerfile contains an unreviewed instruction")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "dockerfile",
        type=Path,
        help="Dockerfile generated by langgraph dockerfile",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    try:
        validate_langgraph_recipe(args.config, args.dockerfile)
    except RecipeValidationError as exc:
        print(f"LangGraph recipe validation failed: {exc}", file=sys.stderr)
        return 2
    print("LangGraph recipe validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
