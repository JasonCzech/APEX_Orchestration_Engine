"""Stub source control: deterministic fixture content for any (repo, path, ref).

Besides the legacy scenario fixture, the stub serves a small, deterministic set
of AIFx context repositories so onboarding preview, launch pinning and prompt
composition all run offline (dev, CI, demos). The repository's role follows its
name: ``global-rules``, ``*-team-documents``, otherwise an ACB
(application-context) repository.
"""

import hashlib
import re

from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import FileContent, RepoRef, SecretValue

_SHA_RE = re.compile(r"[0-9a-f]{40}")

_GLOBAL_RULES = {
    "aifx.yaml": """schema: aifx/v1
role: global-rules
entries:
  - id: evidence-first
    type: rule
    path: rules/evidence-first.md
    phases: all
    title: Evidence-first performance engineering
  - id: workload-modeling
    type: skill
    path: skills/workload-modeling.md
    phases: [test_planning, script_scenario]
    title: Workload modeling
    description: Derive arrival rates, pacing and think time from production traffic evidence.
  - id: prior-cycle-lessons
    type: memory
    path: memories/prior-cycle-lessons.md
    phases: [test_planning, reporting, postmortem]
    title: Lessons from earlier cycles
""",
    "rules/evidence-first.md": """# Evidence-first performance engineering

- Every target, threshold and workload share must cite its evidence source.
- Mark anything without evidence as UNKNOWN and raise it as a blocker.
- Prefer production telemetry over estimates; label estimates explicitly.
""",
    "skills/workload-modeling.md": """# Workload modeling

1. Start from peak-hour transaction counts in production telemetry.
2. Convert to arrival rates per transaction and check the shares total 100%.
3. Choose open or closed load based on how real users arrive.
""",
    "memories/prior-cycle-lessons.md": """# Lessons from earlier cycles

- Checkout p95 regressed twice after cache-tier changes; include cache warm-up in ramp.
- Test data exhaustion ended a soak early; size data for the full duration.
""",
}

_TEAM_DOCUMENTS = {
    "aifx.yaml": """schema: aifx/v1
role: team-documents
entries:
  - id: team-standards
    type: rule
    path: rules/team-standards.md
    phases: all
    title: Team performance standards
  - id: escalation
    type: context
    path: context/escalation.md
    phases: [env_triage, reporting, postmortem]
    title: Escalation and ownership
""",
    "rules/team-standards.md": """# Team performance standards

- Report p95 and p99 latency for every transaction, not averages alone.
- Defect drafts name the owning team and the evidence that justifies them.
""",
    "context/escalation.md": """# Escalation and ownership

- Environment issues go to the platform team's queue before the test resumes.
- Postmortem actions need an owner and a due date.
""",
}

_APPLICATION_CONTEXT = {
    "aifx.yaml": """schema: aifx/v1
role: application-context
entries:
  - id: architecture
    type: context
    path: context/architecture.md
    phases: all
    title: Application architecture
  - id: known-limits
    type: memory
    path: memories/known-limits.md
    phases: [test_planning, env_triage, reporting]
    title: Known capacity limits
""",
    "context/architecture.md": """# Application architecture

- Web tier: stateless storefront behind the regional load balancer.
- Services: catalog, cart and checkout, each with its own datastore.
- Checkout depends on the payment gateway and the inventory service.
""",
    "memories/known-limits.md": """# Known capacity limits

- The inventory service saturates near 1,200 requests per second per region.
- The payment gateway sandbox enforces its own rate limit during tests.
""",
}


def _fixture_files(repo: RepoRef) -> dict[str, str]:
    name = repo.name.rsplit("/", 1)[-1]
    if name == "global-rules":
        return _GLOBAL_RULES
    if name.endswith("-team-documents"):
        return _TEAM_DOCUMENTS
    return _APPLICATION_CONTEXT


@AdapterRegistry.register(PortKind.SOURCE_CONTROL, "stub")
class StubSourceControlAdapter:
    def __init__(
        self, conn: ConnectionConfig | None = None, secret: SecretValue | None = None
    ) -> None:
        self._conn = conn

    async def resolve_commit(self, repo: RepoRef, ref: str = "HEAD") -> str:
        if _SHA_RE.fullmatch(ref):
            return ref
        seed = f"{repo.owner or ''}/{repo.name}@{ref}"
        return hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()

    async def get_file(self, repo: RepoRef, path: str, ref: str = "HEAD") -> FileContent:
        if path == "aifx.yaml" or path.startswith(("rules/", "skills/", "memories/", "context/")):
            files = _fixture_files(repo)
            if path not in files:
                raise KeyError(f"{repo.name}:{path} is not in the stub fixture")
            media_type = "application/yaml" if path.endswith(".yaml") else "text/markdown"
            return FileContent(path=path, ref=ref, text=files[path], media_type=media_type)
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
