"""Stub source control: deterministic fixture file content for any (repo, path, ref)."""

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import FileContent, RepoRef, SecretValue


@AdapterRegistry.register(PortKind.SOURCE_CONTROL, "stub")
class StubSourceControlAdapter:
    def __init__(
        self, conn: ConnectionConfig | None = None, secret: SecretValue | None = None
    ) -> None:
        self._conn = conn

    async def get_file(self, repo: RepoRef, path: str, ref: str = "HEAD") -> FileContent:
        text = (
            f"# {repo.name}:{path}@{ref} (stub fixture)\n"
            "scenario:\n"
            "  name: checkout-baseline\n"
            "  vusers: 10\n"
            "  ramp_s: 5\n"
            "  duration_s: 60\n"
            "  flow:\n"
            "    - GET /catalog\n"
            "    - POST /cart\n"
            "    - POST /checkout\n"
        )
        return FileContent(path=path, ref=ref, text=text, media_type="text/plain")
