"""Validate and normalize the version carried by a release Git tag."""

from __future__ import annotations

import re
import sys

# Docker tag components are limited to 128 characters. Release versions are
# also used as Helm chart/app versions, so validate their shared strict subset:
# SemVer without build metadata and with the canonical numeric forms.
_MAX_DOCKER_TAG_LENGTH = 128
_NUMERIC_IDENTIFIER = r"(?:0|[1-9][0-9]*)"
_PRERELEASE_IDENTIFIER = rf"(?:{_NUMERIC_IDENTIFIER}|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
_RELEASE_VERSION = re.compile(
    rf"{_NUMERIC_IDENTIFIER}\."
    rf"{_NUMERIC_IDENTIFIER}\."
    rf"{_NUMERIC_IDENTIFIER}"
    rf"(?:-{_PRERELEASE_IDENTIFIER}(?:\.{_PRERELEASE_IDENTIFIER})*)?"
)


def normalize_release_tag(tag: str) -> str:
    """Return the release version or reject a non-canonical/unsafe tag."""

    version = tag.removeprefix("v") if tag.startswith("v") else ""
    if (
        not version
        or len(version) > _MAX_DOCKER_TAG_LENGTH
        or _RELEASE_VERSION.fullmatch(version) is None
    ):
        raise ValueError(
            "tag must be v-prefixed canonical SemVer without build metadata and fit in a Docker tag"
        )
    return version


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: release_version.py TAG", file=sys.stderr)
        return 2
    try:
        version = normalize_release_tag(args[0])
    except ValueError as exc:
        print(f"release_version.py: {exc}", file=sys.stderr)
        return 2
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
