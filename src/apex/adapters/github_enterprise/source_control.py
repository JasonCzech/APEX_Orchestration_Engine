"""Read files from GitHub Enterprise repositories as a GitHub App; never write.

Connection options::

    {"base_url": "https://ghe.example.com/api/v3",   # GHES REST root (or https://api.github.com)
     "app_id": "12345",                               # GitHub App ID
     "verify_tls": true}                              # optional, default true

The secret reference resolves to the GitHub App's PEM private key. Each read
uses a short-lived installation token: the adapter signs an RS256 app JWT,
finds the app's installation for the repository, and exchanges it for a token
limited to ``contents: read``. Callers pin reads by resolving a ref to a commit
SHA first; content read at a SHA is immutable and cached for the adapter's life.
Corporate certificate authorities come from ``SSL_CERT_FILE``, like the S3
adapter. Error messages name the repository and path but never echo provider
text, the key, the JWT or a token.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
import re
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import jwt

from apex.adapters.http_resilience import (
    ResponseTooLargeError,
    parse_json_response,
    resilient_request,
    retry_policy,
)
from apex.adapters.network_safety import private_hosts_allowed, safe_async_http_client
from apex.adapters.options import (
    coerce_bool,
    require_bounded_credential,
    require_pem_private_key,
)
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import FileContent, RepoRef, SecretValue

API_VERSION = "2022-11-28"
USER_AGENT = "apex-orchestration-engine"
MAX_FILE_BYTES = 1_000_000
_JSON_RESPONSE_BYTES = 2_000_000  # base64 inflates a 1 MB file by a third, plus envelope
_TIMEOUT_S = 10.0
_JWT_LIFETIME_S = 540  # GitHub accepts at most 10 minutes
_JWT_CLOCK_SKEW_S = 60
_TOKEN_REFRESH_MARGIN_S = 300
_CONTENT_CACHE_ENTRIES = 256

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_REF_RE = re.compile(r"[A-Za-z0-9._/-]{1,255}")
_APP_ID_RE = re.compile(r"[1-9][0-9]{0,19}")
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")


def _name(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _NAME_RE.fullmatch(value) is None or value in {".", ".."}:
        raise ValueError(f"{label} must be a GitHub owner or repository name")
    return value


def validate_ref(value: Any) -> str:
    """Accept a branch, tag or SHA spelled the way git allows; reject traversal shapes."""

    if (
        not isinstance(value, str)
        or _REF_RE.fullmatch(value) is None
        or ".." in value
        or "//" in value
        or value.startswith(("/", "-", "."))
        or value.endswith(("/", ".", ".lock"))
    ):
        raise ValueError("Use a branch, tag or commit SHA without spaces or '..'")
    return value


def validate_repo_path(value: Any) -> str:
    """A relative file path inside the repository: no traversal, no absolute paths."""

    if not isinstance(value, str) or not 1 <= len(value) <= 1_024:
        raise ValueError("Repository paths must be 1-1024 characters")
    if value.startswith("/") or "\\" in value:
        raise ValueError("Repository paths must be relative and use '/' separators")
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("Repository paths cannot contain empty, '.' or '..' segments")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError("Repository paths cannot contain control characters")
    return value


def _api_base(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Configure the GitHub Enterprise API base_url")
    parsed = urlsplit(value.strip())
    path = parsed.path.rstrip("/")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Use the GitHub API URL without credentials, query or fragment")
    if parsed.hostname == "api.github.com":
        if path:
            raise ValueError("GitHub.com's API URL is https://api.github.com")
    elif path != "/api/v3":
        raise ValueError("GitHub Enterprise Server API URLs end in /api/v3")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _media_type(path: str) -> str:
    lowered = path.lower()
    if lowered.endswith((".md", ".markdown")):
        return "text/markdown"
    if lowered.endswith((".yaml", ".yml")):
        return "application/yaml"
    return "text/plain"


class _Unauthorized(RuntimeError):
    """401: the JWT or installation token was rejected (expired, revoked, app reinstalled)."""


class _StaleInstallation(RuntimeError):
    """The cached installation can no longer mint tokens (reinstalled or re-permissioned)."""


@AdapterRegistry.register(PortKind.SOURCE_CONTROL, "github_enterprise")
class GitHubEnterpriseSourceControlAdapter:
    def __init__(self, conn: ConnectionConfig, secret: SecretValue | None) -> None:
        options = conn.options
        self._base = _api_base(options.get("base_url"))
        app_id = options.get("app_id")
        if isinstance(app_id, int) and not isinstance(app_id, bool):
            app_id = str(app_id)
        if not isinstance(app_id, str) or _APP_ID_RE.fullmatch(app_id) is None:
            raise ValueError("Configure the numeric GitHub App app_id")
        self._app_id = app_id
        if secret is None:
            raise ValueError("Configure a secret reference for the GitHub App private key")
        self._key = require_pem_private_key(secret.value, label="GitHub App private key")
        self._verify: str | bool = (
            (os.environ.get("SSL_CERT_FILE") or True)
            if coerce_bool(options.get("verify_tls"), default=True)
            else False
        )
        self._allow_private_hosts = private_hosts_allowed(options)
        self._http: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._installations: dict[str, int] = {}
        self._tokens: dict[int, tuple[str, float]] = {}
        self._content: OrderedDict[tuple[str, str, str, str], FileContent] = OrderedDict()

    # --- transport -----------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._http is None or self._http.is_closed or self._loop is not loop:
            # The resolver caches adapters across graph-node event loops; an
            # AsyncClient is bound to the loop that created it.
            self._http = safe_async_http_client(
                base_url=self._base,
                timeout=_TIMEOUT_S,
                follow_redirects=False,
                verify=self._verify,
                allow_private_hosts=self._allow_private_hosts,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": USER_AGENT,
                    "X-GitHub-Api-Version": API_VERSION,
                },
            )
            self._loop = loop
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
        self._http = None
        self._loop = None

    async def _send(
        self,
        method: str,
        path: str,
        *,
        token: str,
        what: str,
        max_bytes: int = _JSON_RESPONSE_BYTES,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {token}"
        try:
            response = await resilient_request(
                self._client(),
                method,
                path,
                headers=headers,
                max_response_bytes=max_bytes,
                retry=retry_policy(retry_methods={"GET", "POST"}),
                **kwargs,
            )
        except ResponseTooLargeError:
            raise RuntimeError(
                f"GitHub Enterprise returned an oversized response for {what}"
            ) from None
        except httpx.HTTPError:
            raise RuntimeError(
                "GitHub Enterprise connection failed; check network access and retry"
            ) from None
        status = response.status_code
        if status in {200, 201}:
            return response
        if status == 401:
            raise _Unauthorized(
                "GitHub Enterprise rejected the GitHub App credentials; "
                "check the app ID and private key"
            )
        if status in {403, 429}:
            # Primary limits zero the remaining count; secondary (abuse) limits
            # send Retry-After instead.
            if (
                status == 429
                or response.headers.get("x-ratelimit-remaining") == "0"
                or response.headers.get("retry-after") is not None
            ):
                raise RuntimeError("GitHub Enterprise rate limit reached; retry later")
            raise RuntimeError(
                f"GitHub Enterprise denied access to {what}; "
                "check the app's installation and repository permissions"
            )
        if status in {301, 302, 307, 308}:
            raise RuntimeError(f"{what} has moved; update the mapping to the new repository name")
        if status in {404, 422}:
            raise KeyError(f"{what} is unavailable to the GitHub App")
        raise RuntimeError(f"GitHub Enterprise request for {what} failed (HTTP {status})")

    # --- authentication ------------------------------------------------------

    def _app_jwt(self) -> str:
        now = int(time.time())
        return jwt.encode(
            {"iat": now - _JWT_CLOCK_SKEW_S, "exp": now + _JWT_LIFETIME_S, "iss": self._app_id},
            self._key,
            algorithm="RS256",
        )

    async def _installation_id(self, owner: str, repo: str) -> int:
        cached = self._installations.get(owner)
        if cached is not None:
            return cached
        response = await self._send(
            "GET",
            f"/repos/{owner}/{repo}/installation",
            token=self._app_jwt(),
            what=f"{owner}/{repo}",
        )
        data = parse_json_response(response, context="GitHub App installation")
        installation = data.get("id") if isinstance(data, dict) else None
        if type(installation) is not int or installation < 1:
            raise RuntimeError("GitHub Enterprise returned an invalid installation")
        self._installations[owner] = installation
        return installation

    async def _token(self, owner: str, repo: str) -> str:
        installation = await self._installation_id(owner, repo)
        cached = self._tokens.get(installation)
        if cached is not None and cached[1] - _TOKEN_REFRESH_MARGIN_S > time.time():
            return cached[0]
        try:
            response = await self._send(
                "POST",
                f"/app/installations/{installation}/access_tokens",
                token=self._app_jwt(),
                what=f"the GitHub App installation for {owner}",
                json={"permissions": {"contents": "read", "metadata": "read"}},
            )
        except KeyError:
            # 404/422 here means the installation vanished (reinstalled) or cannot
            # grant the requested permissions; it is not a missing file.
            self._forget(owner)
            raise _StaleInstallation(
                f"The GitHub App installation for {owner} cannot grant contents:read; "
                "check the app's repository permissions and installation"
            ) from None
        data = parse_json_response(response, context="GitHub App installation token")
        if not isinstance(data, dict):
            raise RuntimeError("GitHub Enterprise returned an invalid installation token")
        token = require_bounded_credential(
            data.get("token"), label="GitHub installation token", max_bytes=1_024, header_token=True
        )
        expires = data.get("expires_at")
        try:
            expires_at = datetime.fromisoformat(str(expires).replace("Z", "+00:00")).timestamp()
        except ValueError:
            raise RuntimeError(
                "GitHub Enterprise returned an invalid installation token expiry"
            ) from None
        self._tokens[installation] = (token, expires_at)
        return token

    # --- port ------------------------------------------------------------------

    def _forget(self, owner: str) -> None:
        installation = self._installations.pop(owner, None)
        if installation is not None:
            self._tokens.pop(installation, None)

    async def _repo_request(
        self, owner: str, repo: str, method: str, path: str, *, what: str, **kwargs: Any
    ) -> httpx.Response:
        """Send with an installation token; after a 401 (reinstall, revoked token)
        drop the cached installation and token and retry once with fresh ones."""

        try:
            return await self._send(
                method, path, token=await self._token(owner, repo), what=what, **kwargs
            )
        except (_Unauthorized, _StaleInstallation):
            self._forget(owner)
        return await self._send(
            method, path, token=await self._token(owner, repo), what=what, **kwargs
        )

    @staticmethod
    def _repo(repo: RepoRef) -> tuple[str, str]:
        owner = repo.owner
        name = repo.name
        if owner is None and name.count("/") == 1:
            owner, name = name.split("/")
        return _name(owner, label="Repository owner"), _name(name, label="Repository name")

    async def resolve_commit(self, repo: RepoRef, ref: str = "HEAD") -> str:
        owner, name = self._repo(repo)
        ref = validate_ref(ref)
        what = f"{owner}/{name}@{ref}"
        response = await self._repo_request(
            owner,
            name,
            "GET",
            f"/repos/{owner}/{name}/commits/{quote(ref, safe='/')}",
            what=what,
            # A SHA is 40 bytes, but error bodies must still fit so the status maps.
            max_bytes=16_384,
            headers={"Accept": "application/vnd.github.sha"},
        )
        sha = response.text.strip()
        if _SHA_RE.fullmatch(sha) is None:
            raise RuntimeError(f"GitHub Enterprise returned an invalid commit for {what}")
        return sha

    async def get_file(self, repo: RepoRef, path: str, ref: str = "HEAD") -> FileContent:
        owner, name = self._repo(repo)
        path = validate_repo_path(path)
        ref = validate_ref(ref)
        key = (owner, name, ref, path)
        cached = self._content.get(key)
        if cached is not None:
            self._content.move_to_end(key)
            return cached.model_copy(deep=True)
        what = f"{owner}/{name}:{path}@{ref}"
        response = await self._repo_request(
            owner,
            name,
            "GET",
            f"/repos/{owner}/{name}/contents/{quote(path, safe='/')}",
            what=what,
            params={"ref": ref},
        )
        data = parse_json_response(response, context="GitHub file")
        if not isinstance(data, dict) or data.get("type") != "file":
            # Directories come back as lists; symlinks and submodules carry
            # their own types. Only regular files are readable content.
            raise RuntimeError(f"{what} is not a regular file")
        size = data.get("size")
        if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
            raise RuntimeError(f"{what} exceeds the {MAX_FILE_BYTES:,}-byte file limit")
        if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            raise RuntimeError(f"{what} content is unavailable from the contents API")
        try:
            raw = base64.b64decode(data["content"].replace("\n", ""), validate=True)
        except (binascii.Error, ValueError):
            raise RuntimeError(f"{what} returned invalid content encoding") from None
        if len(raw) != size:
            raise RuntimeError(f"{what} content does not match its reported size")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise RuntimeError(f"{what} is not UTF-8 text") from None
        if "\x00" in text:
            raise RuntimeError(f"{what} is binary, not text")
        content = FileContent(path=path, ref=ref, text=text, media_type=_media_type(path))
        if _SHA_RE.fullmatch(ref):
            # Content at a commit SHA never changes; branch reads stay uncached.
            self._content[key] = content
            if len(self._content) > _CONTENT_CACHE_ENTRIES:
                self._content.popitem(last=False)
        return content.model_copy(deep=True)

    async def probe(self) -> str:
        """Authenticate as the app itself, without needing any repository."""

        response = await self._send("GET", "/app", token=self._app_jwt(), what="the GitHub App")
        data = parse_json_response(response, context="GitHub App")
        slug = data.get("slug") if isinstance(data, dict) else None
        if not isinstance(slug, str) or _SLUG_RE.fullmatch(slug) is None:
            raise RuntimeError("GitHub Enterprise returned an invalid GitHub App")
        return f"authenticated as GitHub App {slug}"
