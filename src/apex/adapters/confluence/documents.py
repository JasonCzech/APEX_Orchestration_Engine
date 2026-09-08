"""Read Confluence Cloud pages within a configured space; never publish content."""

import asyncio
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import httpx

from apex.adapters.http_resilience import parse_json_response, resilient_request
from apex.adapters.network_safety import safe_async_http_client
from apex.adapters.options import require_bounded_credential
from apex.adapters.registry import AdapterRegistry, ConnectionConfig, PortKind
from apex.domain.integrations import DocHit, DocRef, DocScope, DocumentContent, SecretValue


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if not self.hidden and tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        if not self.hidden and tag in {"td", "th"}:
            self.parts.append("\t")
        if not self.hidden and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,19}", value) is None:
        raise ValueError(
            "Confluence page, space and revision identifiers must be positive integers"
        )
    return value


@AdapterRegistry.register(PortKind.DOCUMENTS, "confluence")
class ConfluenceDocumentsAdapter:
    def __init__(self, conn: ConnectionConfig, secret: SecretValue | None):
        base = conn.options.get("base_url")
        if not isinstance(base, str):
            raise ValueError("Configure a Confluence Cloud base_url")
        parsed = urlsplit(base)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".atlassian.net")
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.port not in (None, 443)
            or parsed.path not in ("", "/", "/wiki", "/wiki/")
        ):
            raise ValueError(
                "Use the HTTPS Confluence Cloud site URL without credentials or query parameters"
            )
        self._base = f"https://{parsed.hostname}"
        self._space = _identifier(conn.options.get("space_id"))
        email = require_bounded_credential(
            conn.options.get("user_email"), label="Confluence user_email", max_bytes=320
        )
        if ":" in email:
            raise ValueError("Confluence user_email cannot contain a colon")
        if secret is None:
            raise ValueError("Configure a Confluence API-token secret reference")
        token = require_bounded_credential(secret.value, label="Confluence API token")
        self._auth = httpx.BasicAuth(email, token)
        self._http: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _client(self):
        loop = asyncio.get_running_loop()
        if self._http is None or self._http.is_closed or self._loop is not loop:
            self._http = safe_async_http_client(
                base_url=self._base, auth=self._auth, timeout=20, follow_redirects=False
            )
            self._loop = loop
        return self._http

    async def aclose(self):
        if self._http is not None:
            await self._http.aclose()
        self._http = None
        self._loop = None

    async def _get(self, path: str, params: dict[str, Any]):
        try:
            response = await resilient_request(
                self._client(), "GET", path, params=params, max_response_bytes=2_000_000
            )
        except httpx.HTTPError:
            raise RuntimeError("Confluence connection failed; check access and retry") from None
        if response.status_code in {401, 403}:
            raise RuntimeError("Confluence permission denied; check token and space access")
        if response.status_code == 404:
            raise KeyError("Confluence page or space is unavailable")
        if response.status_code != 200:
            raise RuntimeError("Confluence read failed; check provider availability")
        data = parse_json_response(response, context="Confluence document")
        if not isinstance(data, dict):
            raise RuntimeError("Confluence returned an invalid page response")
        return data

    def _ref(self, page: dict[str, Any]) -> DocRef:
        page_id = _identifier(page.get("id"))
        if page.get("spaceId") != self._space:
            raise RuntimeError("Confluence page is outside the configured space")
        version = (
            page.get("version", {}).get("number") if isinstance(page.get("version"), dict) else None
        )
        if type(version) is not int or version < 1:
            raise RuntimeError("Confluence page revision is unavailable")
        return DocRef(
            id=page_id,
            source="confluence",
            revision=str(version),
            uri=f"{self._base}/wiki/spaces/{self._space}/pages/{page_id}",
        )

    async def search(self, query: str, *, scope: DocScope, k: int = 10) -> list[DocHit]:
        if type(k) is not int or not 1 <= k <= 100 or len(query) > 500:
            raise ValueError("Use a title query under 500 characters and 1-100 results")
        if scope.collections and scope.collections != [self._space]:
            raise ValueError("Confluence search is restricted to the configured space")
        params: dict[str, Any] = {"space-id": self._space, "limit": k, "status": "current"}
        if query.strip():
            params["title"] = query.strip()
        data = await self._get("/wiki/api/v2/pages", params)
        pages = data.get("results")
        if not isinstance(pages, list) or len(pages) > k:
            raise RuntimeError("Confluence returned invalid search results")
        hits = []
        seen = set()
        for page in pages:
            if not isinstance(page, dict):
                raise RuntimeError("Confluence returned invalid page metadata")
            ref = self._ref(page)
            if ref.id in seen:
                raise RuntimeError("Confluence returned duplicate page identities")
            seen.add(ref.id)
            title = page.get("title")
            if not isinstance(title, str) or not title.strip() or len(title) > 500:
                raise RuntimeError("Confluence page title is invalid")
            hits.append(
                DocHit(
                    ref=ref,
                    title=title,
                    snippet="Confluence Cloud page; fetch to inspect this revision.",
                )
            )
        return hits

    async def fetch(self, ref: DocRef) -> DocumentContent:
        page_id = _identifier(ref.id)
        if ref.source != "confluence":
            raise ValueError("Select a Confluence page reference")
        expected_uri = f"{self._base}/wiki/spaces/{self._space}/pages/{page_id}"
        if ref.uri is not None and ref.uri != expected_uri:
            raise ValueError("Confluence reference belongs to a different site or space")
        params: dict[str, Any] = {"body-format": "storage", "status": "current"}
        if ref.revision is not None:
            params["version"] = _identifier(ref.revision)
        data = await self._get(f"/wiki/api/v2/pages/{page_id}", params)
        actual = self._ref(data)
        if actual.id != page_id or (ref.revision is not None and actual.revision != ref.revision):
            raise RuntimeError(
                "Confluence returned different page/version evidence; refresh the source"
            )
        body = data.get("body")
        storage = body.get("storage") if isinstance(body, dict) else None
        value = storage.get("value") if isinstance(storage, dict) else None
        if not isinstance(value, str):
            raise RuntimeError("Confluence page body is unavailable")
        if "<ac:structured-macro" in value.lower() or "<ri:attachment" in value.lower():
            raise RuntimeError(
                "Confluence macros or attachments require separate evidence retrieval"
            )
        parser = _PlainText()
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts).strip()
        if not text:
            raise RuntimeError(
                "Confluence page has no readable text; "
                "macros and attachments may require separate retrieval"
            )
        title = data.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise RuntimeError("Confluence page title is unavailable")
        return DocumentContent(ref=actual, title=title, media_type="text/plain", text=text)
