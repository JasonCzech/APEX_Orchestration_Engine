"""Source-control port (GitHub Enterprise / stub) — read-only, commit-pinned file access."""

from typing import Protocol, runtime_checkable

from apex.domain.integrations import FileContent, RepoRef


@runtime_checkable
class SourceControlPort(Protocol):
    async def resolve_commit(self, repo: RepoRef, ref: str = "HEAD") -> str:
        """Resolve a branch, tag or SHA to the 40-hex commit SHA it names right now."""
        ...

    async def get_file(self, repo: RepoRef, path: str, ref: str = "HEAD") -> FileContent:
        """Read one file. Pass a resolved commit SHA as ``ref`` for a reproducible read."""
        ...
