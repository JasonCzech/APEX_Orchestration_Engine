"""Select the immutable digest for one exact repository from Docker inspect JSON."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}")
MAX_REPO_DIGEST_BYTES = 1_048_576


def select_repo_digest(repository: str, raw: str) -> str:
    repository = repository.rstrip("/")
    if (
        _REPOSITORY.fullmatch(repository) is None
        or "://" in repository
        or any(part in {"", ".", ".."} for part in repository.split("/"))
    ):
        # The repository may originate in a secret-backed registry setting;
        # never reflect a malformed value into CI diagnostics.
        raise ValueError("repository must be a valid bounded Docker repository name")
    if len(raw.encode("utf-8")) > MAX_REPO_DIGEST_BYTES:
        raise ValueError("Docker RepoDigests output exceeds the size limit")
    try:
        values: Any = json.loads(raw)
    except json.JSONDecodeError:
        # Do not retain the raw Docker output in an exception cause. Registry
        # tooling may echo credential-bearing references in malformed output.
        raise ValueError("Docker RepoDigests output is not valid JSON") from None
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("Docker RepoDigests output must be a JSON string array")
    prefix = f"{repository}@"
    matches = sorted(set(value for value in values if value.startswith(prefix)))
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one RepoDigest for the requested repository; found {len(matches)}"
        )
    digest = matches[0].removeprefix(prefix)
    if not _DIGEST.fullmatch(digest):
        raise ValueError("Docker returned an invalid digest for the requested repository")
    return matches[0]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: docker_repo_digest.py REPOSITORY", file=sys.stderr)
        return 2
    try:
        raw = sys.stdin.read(MAX_REPO_DIGEST_BYTES + 1)
        print(select_repo_digest(argv[0], raw))
    except ValueError as exc:
        print(f"docker_repo_digest.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
