"""Cross-platform development tasks for APEX.

This intentionally mirrors the Makefile with only Python stdlib so Windows
developers can use the same backend workflow from PowerShell or Command Prompt.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

Task = Callable[[list[str]], None]
TASKS: dict[str, tuple[str, Task]] = {}


def task(name: str, help_text: str) -> Callable[[Task], Task]:
    def register(func: Task) -> Task:
        TASKS[name] = (help_text, func)
        return func

    return register


def run(command: list[str]) -> None:
    print("+ " + " ".join(command), flush=True)
    try:
        subprocess.run(command, cwd=REPO_ROOT, check=True)
    except FileNotFoundError:
        raise SystemExit(
            f"Required executable not found: {command[0]}. "
            "Install it and make sure it is available on PATH."
        ) from None
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from None


@task("ensure-env", "Copy .env.example to .env if .env does not exist.")
def ensure_env(_: list[str]) -> None:
    if ENV_FILE.exists():
        print(".env already exists")
        return
    if not ENV_EXAMPLE.exists():
        raise SystemExit(".env.example is missing")
    shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
    print("Created .env from .env.example")


@task("infra-up", "Start dev Postgres, Redis, and MinIO with Docker Compose.")
def infra_up(_: list[str]) -> None:
    run(["docker", "compose", "-f", "docker-compose.dev.yaml", "up", "-d", "--wait"])


@task("infra-down", "Stop the dev Docker Compose services.")
def infra_down(_: list[str]) -> None:
    run(["docker", "compose", "-f", "docker-compose.dev.yaml", "down"])


@task("migrate", "Apply apex-schema migrations.")
def migrate(_: list[str]) -> None:
    run(["uv", "run", "alembic", "upgrade", "head"])


@task("seed", "Seed API consumers, prompts, catalog rows, and stub connections.")
def seed(_: list[str]) -> None:
    for script in ("seed_dev.py", "seed_prompts.py", "seed_catalog.py"):
        run(["uv", "run", "python", f"scripts/{script}"])


@task("setup", "Create .env, start infra, migrate, and seed. Run `uv sync` first.")
def setup(_: list[str]) -> None:
    ensure_env([])
    infra_up([])
    migrate([])
    seed([])


@task("dev", "Run the LangGraph dev server on port 2024.")
def dev(extra_args: list[str]) -> None:
    run(["uv", "run", "langgraph", "dev", "--no-browser", *extra_args])


@task("test", "Run the Python test suite.")
def test(_: list[str]) -> None:
    run(["uv", "run", "pytest"])


@task("lint", "Run ruff lint and format checks.")
def lint(_: list[str]) -> None:
    run(["uv", "run", "ruff", "check", "."])
    run(["uv", "run", "ruff", "format", "--check", "."])


@task("typecheck", "Run pyright.")
def typecheck(_: list[str]) -> None:
    run(["uv", "run", "pyright"])


@task("check", "Run backend lint, typecheck, and tests.")
def check(_: list[str]) -> None:
    lint([])
    typecheck([])
    test([])


@task("openapi", "Export the committed /v1 OpenAPI spec.")
def openapi(_: list[str]) -> None:
    run(["uv", "run", "python", "scripts/export_openapi.py"])


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="APEX development task runner")
    parser.add_argument("task", nargs="?", choices=sorted(TASKS))
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def print_tasks() -> None:
    print("Available tasks:")
    for name in sorted(TASKS):
        help_text, _ = TASKS[name]
        print(f"  {name:<12} {help_text}")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.task is None:
        print_tasks()
        return 0
    extra = args.extra
    if extra[:1] == ["--"]:
        extra = extra[1:]
    TASKS[args.task][1](extra)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
