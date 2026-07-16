import pytest
from scripts.docker_repo_digest import MAX_REPO_DIGEST_BYTES, select_repo_digest


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


def test_invalid_repo_digest_json_does_not_retain_the_raw_document() -> None:
    canary = "registry-output-secret-canary"

    with pytest.raises(ValueError) as caught:
        select_repo_digest("registry.example/apex", f'{{"token":"{canary}"')

    assert caught.value.__cause__ is None
    assert canary not in repr(caught.value)


def test_repo_digest_input_is_bounded_before_json_parsing() -> None:
    with pytest.raises(ValueError, match="size limit") as caught:
        select_repo_digest("registry.example/apex", "x" * (MAX_REPO_DIGEST_BYTES + 1))

    assert caught.value.__cause__ is None


def test_invalid_repository_is_not_reflected_into_diagnostics() -> None:
    canary = "registry-userinfo-secret-canary"

    with pytest.raises(ValueError) as caught:
        select_repo_digest(f"https://user:{canary}@registry.example/apex", "[]")

    assert canary not in str(caught.value)
