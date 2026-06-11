"""Source-control port (GitHub / stub) — read-only file access for script refs."""

from typing import Protocol, runtime_checkable

from apex.domain.integrations import FileContent, RepoRef


@runtime_checkable
class SourceControlPort(Protocol):
    async def get_file(self, repo: RepoRef, path: str, ref: str = "HEAD") -> FileContent: ...
