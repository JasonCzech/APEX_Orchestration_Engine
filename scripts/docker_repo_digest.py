"""Select the immutable digest for one exact repository from Docker inspect JSON."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def select_repo_digest(repository: str, raw: str) -> str:
    repository = repository.rstrip("/")
    if not repository or "@" in repository:
        raise ValueError("repository must be a non-empty Docker repository name")
    try:
        values: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Docker RepoDigests output is not valid JSON") from exc
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("Docker RepoDigests output must be a JSON string array")
    prefix = f"{repository}@"
    matches = sorted(set(value for value in values if value.startswith(prefix)))
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one RepoDigest for {repository!r}, found {len(matches)}"
        )
    digest = matches[0].removeprefix(prefix)
    if not _DIGEST.fullmatch(digest):
        raise ValueError(f"Docker returned an invalid digest for {repository!r}")
    return matches[0]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: docker_repo_digest.py REPOSITORY", file=sys.stderr)
        return 2
    try:
        print(select_repo_digest(argv[0], sys.stdin.read()))
    except ValueError as exc:
        print(f"docker_repo_digest.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
