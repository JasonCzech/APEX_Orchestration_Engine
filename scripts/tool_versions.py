"""Canonical infrastructure tool versions used by CI, deploy, and release jobs."""

from __future__ import annotations

import re
from collections.abc import Mapping

TOOL_VERSIONS = {
    "terraform": "1.15.6",
    "helm": "4.2.1",
    "kubeconform": "0.6.7",
}
TOOL_CHECKSUMS = {
    "kubeconform_linux_amd64_sha256": (
        "95f14e87aa28c09d5941f11bd024c1d02fdc0303ccaa23f61cef67bc92619d73"
    ),
}

_EXPECTED_TOOLS = ("terraform", "helm", "kubeconform")
_EXPECTED_CHECKSUMS = ("kubeconform_linux_amd64_sha256",)
_SEMANTIC_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def github_outputs(
    versions: Mapping[str, str] = TOOL_VERSIONS,
    checksums: Mapping[str, str] = TOOL_CHECKSUMS,
) -> str:
    """Render validated values for appending to ``GITHUB_OUTPUT``."""
    if tuple(versions) != _EXPECTED_TOOLS:
        raise ValueError(f"expected exactly these ordered tools: {_EXPECTED_TOOLS!r}")

    lines: list[str] = []
    for tool in _EXPECTED_TOOLS:
        version = versions[tool]
        if _SEMANTIC_VERSION.fullmatch(version) is None:
            raise ValueError(f"invalid {tool} version: {version!r}")
        lines.append(f"{tool}={version}")
    if tuple(checksums) != _EXPECTED_CHECKSUMS:
        raise ValueError(f"expected exactly these ordered checksums: {_EXPECTED_CHECKSUMS!r}")
    for artifact in _EXPECTED_CHECKSUMS:
        checksum = checksums[artifact]
        if _SHA256.fullmatch(checksum) is None:
            raise ValueError(f"invalid {artifact} checksum")
        lines.append(f"{artifact}={checksum}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(github_outputs())
