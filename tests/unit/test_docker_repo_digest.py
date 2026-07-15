import pytest
from scripts.docker_repo_digest import select_repo_digest


def test_select_repo_digest_uses_the_exact_requested_repository() -> None:
    digest = "a" * 64
    other = "b" * 64
    raw = f'["registry.example/other@sha256:{other}","registry.example/apex@sha256:{digest}"]'

    assert select_repo_digest("registry.example/apex", raw) == (
        f"registry.example/apex@sha256:{digest}"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "null",
        "{}",
        "[]",
        '["registry.example/other@sha256:' + "a" * 64 + '"]',
        '["registry.example/apex@sha256:not-a-digest"]',
    ],
)
def test_select_repo_digest_fails_closed_on_missing_or_malformed_data(raw: str) -> None:
    with pytest.raises(ValueError):
        select_repo_digest("registry.example/apex", raw)
