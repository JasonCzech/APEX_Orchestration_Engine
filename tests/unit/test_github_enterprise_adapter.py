import base64
import json
import time
from collections.abc import Sequence

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from apex.adapters.github_enterprise.source_control import (
    GitHubEnterpriseSourceControlAdapter,
    validate_ref,
    validate_repo_path,
)
from apex.adapters.options import require_pem_private_key
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.diagnostics import contains_credential_material
from apex.domain.integrations import RepoRef, SecretValue
from apex.ports import SourceControlPort

BASE = "https://ghe.example.test/api/v3"
SHA = "0123456789abcdef0123456789abcdef01234567"
TOKEN = "ghs_" + "A" * 36

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode("ascii")


def conn(**options):
    return ConnectionConfig(
        id="ghe",
        name="GHE",
        kind=PortKind.SOURCE_CONTROL,
        provider="github_enterprise",
        options={"base_url": BASE, "app_id": "4242", **options},
    )


def adapter(**options):
    return GitHubEnterpriseSourceControlAdapter(conn(**options), SecretValue(value=PEM))


def expires_in(seconds: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))


def mock_auth(expires: int = 3600):
    installation = respx.get(f"{BASE}/repos/aifx/global-rules/installation").mock(
        return_value=httpx.Response(200, json={"id": 77})
    )
    token = respx.post(f"{BASE}/app/installations/77/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": TOKEN, "expires_at": expires_in(expires)})
    )
    return installation, token


def file_body(text: str, *, kind: str = "file", size: int | None = None):
    raw = text.encode("utf-8")
    return {
        "type": kind,
        "encoding": "base64",
        "size": len(raw) if size is None else size,
        "content": base64.b64encode(raw).decode("ascii"),
        "path": "aifx.yaml",
    }


REPO = RepoRef(owner="aifx", name="global-rules")


def test_registered_and_satisfies_port():
    assert "github_enterprise" in AdapterRegistry.providers_for(PortKind.SOURCE_CONTROL)
    assert isinstance(adapter(), SourceControlPort)


def test_pem_key_accepts_escaped_newlines_and_rejects_bad_keys():
    assert require_pem_private_key(PEM, label="key").key_size == 2048
    assert require_pem_private_key(PEM.replace("\n", "\\n"), label="key").key_size == 2048
    small = rsa.generate_private_key(public_exponent=65537, key_size=1024).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.raises(ValueError, match="at least 2048"):
        require_pem_private_key(small.decode("ascii"), label="key")
    garbled = PEM.replace("A", "?", 5)
    with pytest.raises(ValueError, match="RSA private key") as exc:
        require_pem_private_key(garbled, label="key")
    assert "BEGIN" not in str(exc.value)


@pytest.mark.parametrize(
    "options",
    [
        {"base_url": "https://ghe.example.test/"},
        {"base_url": "https://user:pw@ghe.example.test/api/v3"},
        {"base_url": "https://ghe.example.test/api/v3?x=1"},
        {"base_url": "https://api.github.com/api/v3"},
        {"app_id": "abc"},
        {"app_id": "0"},
    ],
)
def test_rejects_invalid_options_before_io(options):
    with pytest.raises(ValueError):
        adapter(**options)


def test_requires_private_key_secret():
    with pytest.raises(ValueError, match="private key"):
        GitHubEnterpriseSourceControlAdapter(conn(), None)


def test_github_dot_com_base_is_accepted():
    adapter(base_url="https://api.github.com")


@pytest.mark.parametrize("ref", ["../main", "main..x", "/main", "main/", "a b", "x.lock", "-rf"])
def test_ref_validation(ref):
    with pytest.raises(ValueError):
        validate_ref(ref)


@pytest.mark.parametrize("path", ["../x.md", "/etc/passwd", "a//b.md", "a/./b.md", "a\\b.md"])
def test_path_validation(path):
    with pytest.raises(ValueError):
        validate_repo_path(path)


@respx.mock
async def test_resolve_commit_signs_app_jwt_and_caches_the_installation_token():
    installation, token = mock_auth()
    commits = respx.get(f"{BASE}/repos/aifx/global-rules/commits/main").mock(
        return_value=httpx.Response(200, text=SHA + "\n")
    )
    client = adapter()

    assert await client.resolve_commit(REPO, "main") == SHA
    assert await client.resolve_commit(REPO, "main") == SHA

    assert installation.call_count == 1
    assert token.call_count == 1
    assert commits.call_count == 2
    app_jwt = installation.calls.last.request.headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(app_jwt, _KEY.public_key(), algorithms=["RS256"])
    assert claims["iss"] == "4242"
    assert claims["exp"] - claims["iat"] <= 600
    assert claims["iat"] <= time.time() - 30
    assert json.loads(token.calls.last.request.content) == {
        "permissions": {"contents": "read", "metadata": "read"}
    }
    request = commits.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Accept"] == "application/vnd.github.sha"
    assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
    await client.aclose()


@respx.mock
async def test_expiring_installation_token_is_refreshed():
    _, token = mock_auth(expires=60)
    respx.get(f"{BASE}/repos/aifx/global-rules/commits/main").mock(
        return_value=httpx.Response(200, text=SHA)
    )
    client = adapter()
    await client.resolve_commit(REPO, "main")
    await client.resolve_commit(REPO, "main")
    assert token.call_count == 2
    await client.aclose()


@respx.mock
async def test_get_file_decodes_content_and_caches_reads_at_a_commit():
    mock_auth()
    contents = respx.get(f"{BASE}/repos/aifx/global-rules/contents/rules/a%20b.md").mock(
        return_value=httpx.Response(200, json=file_body("# Rules\n- cite evidence\n"))
    )
    client = adapter()

    first = await client.get_file(REPO, "rules/a b.md", SHA)
    second = await client.get_file(REPO, "rules/a b.md", SHA)

    assert first.text == "# Rules\n- cite evidence\n"
    assert first.media_type == "text/markdown"
    assert first.ref == SHA
    assert second == first
    assert contents.call_count == 1
    assert contents.calls.last.request.url.params["ref"] == SHA
    await client.aclose()


@respx.mock
async def test_branch_reads_are_not_cached():
    mock_auth()
    contents = respx.get(f"{BASE}/repos/aifx/global-rules/contents/aifx.yaml").mock(
        return_value=httpx.Response(200, json=file_body("schema: aifx/v1\n"))
    )
    client = adapter()
    await client.get_file(REPO, "aifx.yaml", "main")
    await client.get_file(REPO, "aifx.yaml", "main")
    assert contents.call_count == 2
    await client.aclose()


@respx.mock
@pytest.mark.parametrize(
    ("body", "message"),
    [
        (file_body("x", kind="symlink"), "not a regular file"),
        (file_body("x", size=2_000_000), "file limit"),
        (file_body("x", size=5), "reported size"),
        (file_body("a\x00b"), "binary"),
    ],
)
async def test_get_file_rejects_unsafe_content(body, message):
    mock_auth()
    respx.get(f"{BASE}/repos/aifx/global-rules/contents/aifx.yaml").mock(
        return_value=httpx.Response(200, json=body)
    )
    client = adapter()
    with pytest.raises(RuntimeError, match=message):
        await client.get_file(REPO, "aifx.yaml", SHA)
    await client.aclose()


@respx.mock
async def test_directory_listing_is_rejected():
    mock_auth()
    respx.get(f"{BASE}/repos/aifx/global-rules/contents/rules").mock(
        return_value=httpx.Response(200, json=[{"type": "file", "path": "rules/a.md"}])
    )
    client = adapter()
    with pytest.raises(RuntimeError, match="not a regular file"):
        await client.get_file(REPO, "rules", SHA)
    await client.aclose()


@respx.mock
@pytest.mark.parametrize(
    ("status", "headers", "error", "message"),
    [
        (401, {}, RuntimeError, "credentials"),
        (403, {}, RuntimeError, "denied access"),
        (403, {"x-ratelimit-remaining": "0"}, RuntimeError, "rate limit"),
        (404, {}, KeyError, "unavailable"),
        (422, {}, KeyError, "unavailable"),
        (301, {"location": f"{BASE}/repos/aifx/renamed"}, RuntimeError, "has moved"),
        (500, {}, RuntimeError, "HTTP 500"),
    ],
)
async def test_errors_are_mapped_without_echoing_secrets(status, headers, error, message):
    mock_auth()
    respx.get(f"{BASE}/repos/aifx/global-rules/commits/main").mock(
        return_value=httpx.Response(
            status, headers=headers, text=f"provider says {TOKEN} -----BEGIN PRIVATE KEY-----"
        )
    )
    client = adapter()
    with pytest.raises(error, match=message) as exc:
        await client.resolve_commit(REPO, "main")
    text = str(exc.value)
    assert TOKEN not in text
    assert "provider says" not in text
    assert not contains_credential_material(text)
    await client.aclose()


@respx.mock
async def test_app_not_installed_on_owner_maps_to_unavailable():
    respx.get(f"{BASE}/repos/aifx/global-rules/installation").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    client = adapter()
    with pytest.raises(KeyError, match="aifx/global-rules"):
        await client.resolve_commit(REPO, "main")
    await client.aclose()


async def test_owner_can_be_given_in_the_repository_name():
    client = adapter()
    assert client._repo(RepoRef(name="aifx/global-rules")) == ("aifx", "global-rules")
    with pytest.raises(ValueError):
        client._repo(RepoRef(name="global-rules"))


@respx.mock
async def test_probe_authenticates_as_the_app():
    app = respx.get(f"{BASE}/app").mock(
        return_value=httpx.Response(200, json={"slug": "apex-aifx-reader", "id": 4242})
    )
    client = adapter()
    assert await client.probe() == "authenticated as GitHub App apex-aifx-reader"
    assert app.calls.last.request.headers["Authorization"].startswith("Bearer ey")
    await client.aclose()


# --- regression: credential refresh, stale installations, rate limits, bounds ---

TOKEN_2 = "ghs_" + "B" * 36
LEAK = f"provider says {TOKEN} -----BEGIN PRIVATE KEY-----"
COMMITS = f"{BASE}/repos/aifx/global-rules/commits/main"
INSTALLATION = f"{BASE}/repos/aifx/global-rules/installation"


def token_response(token: str = TOKEN) -> httpx.Response:
    return httpx.Response(201, json={"token": token, "expires_at": expires_in(3600)})


def app_jwts(route) -> list[str]:
    return [call.request.headers["Authorization"].removeprefix("Bearer ") for call in route.calls]


def assert_no_secrets(exc: BaseException, *, jwts: Sequence[str] = ()) -> None:
    text = str(exc)
    pem_body = "".join(PEM.splitlines()[1:-1])
    assert TOKEN not in text
    assert TOKEN_2 not in text
    assert "provider says" not in text
    assert "PRIVATE KEY" not in text
    assert pem_body[:64] not in text
    for value in jwts:
        assert value not in text
    assert not contains_credential_material(text)


@respx.mock
async def test_401_with_cached_token_forgets_installation_and_retries_with_fresh_token():
    installation = respx.get(INSTALLATION).mock(return_value=httpx.Response(200, json={"id": 77}))
    token = respx.post(f"{BASE}/app/installations/77/access_tokens").mock(
        side_effect=[token_response(TOKEN), token_response(TOKEN_2)]
    )
    commits = respx.get(COMMITS).mock(
        side_effect=[
            httpx.Response(200, text=SHA),
            httpx.Response(401, text=LEAK),
            httpx.Response(200, text=SHA),
        ]
    )
    client = adapter()

    assert await client.resolve_commit(REPO, "main") == SHA  # caches installation + token
    assert await client.resolve_commit(REPO, "main") == SHA  # 401 -> re-mint -> success

    assert installation.call_count == 2
    assert token.call_count == 2
    assert commits.call_count == 3
    assert commits.calls[1].request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert commits.calls[2].request.headers["Authorization"] == f"Bearer {TOKEN_2}"
    assert client._tokens[77][0] == TOKEN_2
    await client.aclose()


@respx.mock
async def test_repeated_401_retries_only_once_then_surfaces_a_credentials_error():
    installation = respx.get(INSTALLATION).mock(return_value=httpx.Response(200, json={"id": 77}))
    token = respx.post(f"{BASE}/app/installations/77/access_tokens").mock(
        side_effect=[token_response(TOKEN), token_response(TOKEN_2)]
    )
    commits = respx.get(COMMITS).mock(return_value=httpx.Response(401, text=LEAK))
    client = adapter()

    with pytest.raises(RuntimeError, match="credentials") as exc:
        await client.resolve_commit(REPO, "main")

    assert commits.call_count == 2
    assert installation.call_count == 2
    assert token.call_count == 2
    assert_no_secrets(exc.value, jwts=app_jwts(installation) + app_jwts(token))
    await client.aclose()


@respx.mock
async def test_401_on_token_mint_is_retried_once():
    installation = respx.get(INSTALLATION).mock(return_value=httpx.Response(200, json={"id": 77}))
    token = respx.post(f"{BASE}/app/installations/77/access_tokens").mock(
        return_value=httpx.Response(401, text=LEAK)
    )
    client = adapter()

    with pytest.raises(RuntimeError, match="credentials") as exc:
        await client.resolve_commit(REPO, "main")

    assert token.call_count == 2
    assert installation.call_count == 2
    assert_no_secrets(exc.value, jwts=app_jwts(installation) + app_jwts(token))
    await client.aclose()


@respx.mock
@pytest.mark.parametrize("status", [404, 422])
async def test_stale_installation_is_forgotten_and_retried_once(status):
    installation = respx.get(INSTALLATION).mock(
        side_effect=[
            httpx.Response(200, json={"id": 77}),
            httpx.Response(200, json={"id": 88}),  # app was reinstalled
        ]
    )
    stale = respx.post(f"{BASE}/app/installations/77/access_tokens").mock(
        return_value=httpx.Response(status, text=LEAK)
    )
    fresh = respx.post(f"{BASE}/app/installations/88/access_tokens").mock(
        return_value=token_response(TOKEN_2)
    )
    commits = respx.get(COMMITS).mock(return_value=httpx.Response(200, text=SHA))
    client = adapter()

    assert await client.resolve_commit(REPO, "main") == SHA

    assert installation.call_count == 2
    assert stale.call_count == 1
    assert fresh.call_count == 1
    assert commits.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN_2}"
    assert client._installations == {"aifx": 88}
    assert 77 not in client._tokens
    await client.aclose()


@respx.mock
@pytest.mark.parametrize("status", [404, 422])
async def test_installation_that_cannot_grant_permissions_reports_a_clear_message(status):
    installation = respx.get(INSTALLATION).mock(return_value=httpx.Response(200, json={"id": 77}))
    token = respx.post(f"{BASE}/app/installations/77/access_tokens").mock(
        return_value=httpx.Response(
            status, json={"message": f"permission not granted {TOKEN}"}, headers={"x-x": "1"}
        )
    )
    contents = respx.get(f"{BASE}/repos/aifx/global-rules/contents/aifx.yaml").mock(
        return_value=httpx.Response(200, json=file_body("schema: aifx/v1\n"))
    )
    client = adapter()

    with pytest.raises(RuntimeError, match="cannot grant contents:read") as exc:
        await client.get_file(REPO, "aifx.yaml", SHA)

    assert not isinstance(exc.value, KeyError)
    assert "aifx.yaml" not in str(exc.value)
    assert "unavailable" not in str(exc.value)
    assert "installation for aifx" in str(exc.value)
    assert token.call_count == 2  # one retry, then surfaced
    assert installation.call_count == 2
    assert contents.call_count == 0
    assert client._installations == {}
    assert client._tokens == {}
    assert_no_secrets(exc.value, jwts=app_jwts(installation) + app_jwts(token))
    await client.aclose()


@respx.mock
async def test_token_mint_denied_names_the_installation_not_the_file():
    respx.get(INSTALLATION).mock(return_value=httpx.Response(200, json={"id": 77}))
    respx.post(f"{BASE}/app/installations/77/access_tokens").mock(
        return_value=httpx.Response(403, text=LEAK)
    )
    client = adapter()

    with pytest.raises(RuntimeError, match="denied access to the GitHub App installation") as exc:
        await client.get_file(REPO, "aifx.yaml", SHA)

    assert not isinstance(exc.value, KeyError)
    assert "aifx.yaml" not in str(exc.value)
    assert_no_secrets(exc.value)
    await client.aclose()


@respx.mock
@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (403, {"x-ratelimit-remaining": "0"}),
        (403, {"retry-after": "60"}),
        (403, {"retry-after": "0", "x-ratelimit-remaining": "4999"}),
        (429, {"retry-after": "0"}),
        (429, {"x-ratelimit-remaining": "0", "retry-after": "0"}),
    ],
)
async def test_rate_limits_are_reported_as_rate_limits(status, headers):
    mock_auth()
    respx.get(COMMITS).mock(return_value=httpx.Response(status, headers=headers, text=LEAK))
    client = adapter()

    with pytest.raises(RuntimeError, match="rate limit reached") as exc:
        await client.resolve_commit(REPO, "main")

    text = str(exc.value)
    assert "denied access" not in text
    assert "connection failed" not in text
    assert_no_secrets(exc.value)
    await client.aclose()


@respx.mock
async def test_rate_limited_token_mint_is_reported_as_a_rate_limit():
    respx.get(INSTALLATION).mock(return_value=httpx.Response(200, json={"id": 77}))
    respx.post(f"{BASE}/app/installations/77/access_tokens").mock(
        return_value=httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, text=LEAK)
    )
    client = adapter()
    with pytest.raises(RuntimeError, match="rate limit reached") as exc:
        await client.resolve_commit(REPO, "main")
    assert_no_secrets(exc.value)
    await client.aclose()


@respx.mock
async def test_403_without_rate_limit_signals_is_still_a_permission_denial():
    mock_auth()
    respx.get(COMMITS).mock(
        return_value=httpx.Response(403, headers={"x-ratelimit-remaining": "4999"}, text=LEAK)
    )
    client = adapter()
    with pytest.raises(RuntimeError, match="denied access") as exc:
        await client.resolve_commit(REPO, "main")
    assert "rate limit" not in str(exc.value)
    await client.aclose()


@respx.mock
@pytest.mark.parametrize("status", [404, 422])
async def test_resolve_commit_maps_kilobyte_error_bodies_to_status(status):
    mock_auth()
    body = {"message": "No commit found for SHA: main", "padding": "x" * 8_000, "leak": TOKEN}
    respx.get(COMMITS).mock(return_value=httpx.Response(status, json=body))
    client = adapter()

    with pytest.raises(KeyError, match="unavailable") as exc:
        await client.resolve_commit(REPO, "main")

    assert "oversized" not in str(exc.value)
    assert_no_secrets(exc.value)
    await client.aclose()


@respx.mock
async def test_resolve_commit_body_just_under_the_bound_is_not_oversized():
    mock_auth()
    respx.get(COMMITS).mock(return_value=httpx.Response(404, text="x" * 16_384))
    client = adapter()
    with pytest.raises(KeyError, match="unavailable"):
        await client.resolve_commit(REPO, "main")
    await client.aclose()


@respx.mock
@pytest.mark.parametrize("status", [200, 404])
async def test_resolve_commit_oversized_response_is_reported(status):
    mock_auth()
    respx.get(COMMITS).mock(return_value=httpx.Response(status, text=SHA + " " * 16_385 + LEAK))
    client = adapter()

    with pytest.raises(RuntimeError, match="oversized response for aifx/global-rules@main") as exc:
        await client.resolve_commit(REPO, "main")

    assert_no_secrets(exc.value)
    await client.aclose()


@respx.mock
async def test_no_error_path_leaks_the_pem_jwt_or_token():
    """Walk 401 -> stale -> rate limit -> transport failure; no message carries secrets."""

    installation = respx.get(INSTALLATION).mock(return_value=httpx.Response(200, json={"id": 77}))
    token = respx.post(f"{BASE}/app/installations/77/access_tokens").mock(
        return_value=token_response(TOKEN)
    )
    commits = respx.get(COMMITS)
    client = adapter()
    errors: list[BaseException] = []
    for response in (
        httpx.Response(401, text=LEAK),
        httpx.Response(403, headers={"retry-after": "1"}, text=LEAK),
        httpx.Response(500, text=LEAK),
        httpx.Response(418, text=LEAK),
    ):
        commits.mock(return_value=response)
        with pytest.raises((RuntimeError, KeyError)) as exc:
            await client.resolve_commit(REPO, "main")
        errors.append(exc.value)
    commits.mock(side_effect=httpx.ConnectError(f"boom {TOKEN}"))
    with pytest.raises(RuntimeError, match="connection failed") as exc:
        await client.resolve_commit(REPO, "main")
    errors.append(exc.value)

    jwts = app_jwts(installation) + app_jwts(token)
    assert jwts
    for error in errors:
        assert_no_secrets(error, jwts=jwts)
        assert error.__cause__ is None or not contains_credential_material(str(error.__cause__))
    await client.aclose()
