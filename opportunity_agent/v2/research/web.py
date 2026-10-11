"""Bounded Tavily MCP transport and independently verified official-page reading."""
from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import AsyncExitStack
from datetime import timedelta
from urllib.parse import urlparse

import httpx

from ...official_research import OfficialDomainRegistry, _assert_public_host, _TextParser, _page_title
from ...config import tavily_api_key
from ..core.telemetry import span


class ResearchTextParser(_TextParser):
    """Extract article text while excluding site chrome and hidden menu trees."""

    _SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "aside", "form", "button",
                  "template", "iframe", "head"}
    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
                  "source", "track", "wbr"}
    _BLOCK_TAGS = {"address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption", "figure",
                   "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "ol", "p", "pre",
                   "section", "table", "td", "th", "tr", "ul"}
    _NOISE_TOKEN = re.compile(
        r"(?:^|[-_])(?:tbm|menu|menus|nav|navigation|navbar|breadcrumb|breadcrumbs|cookie|consent|social|share|"
        r"utility|site-header|site-footer|skip-link|pagination)(?:$|[-_])", re.I)
    _MAIN_TOKEN = re.compile(r"^(?:main|main-content|content-main|primary-content|article-content|page-content)$", re.I)

    def __init__(self):
        super().__init__()
        self.heading, self.heading_parts = None, []
        self._ignored_depth = 0
        self._main_roots = []
        self.main_parts = []
        self.has_main_content = False

    def _append(self, value):
        self.parts.append(value)
        if self._main_roots:
            self.main_parts.append(value)

    def _boundary(self):
        target = self.main_parts if self._main_roots else self.parts
        if target and target[-1] != "\n":
            self._append("\n")

    @classmethod
    def _is_noise(cls, tag, attrs, in_main):
        values = dict(attrs)
        role = str(values.get("role", "")).casefold()
        if tag in cls._SKIP_TAGS or role in {"navigation", "menu", "contentinfo"}:
            return True
        if tag == "header" and not in_main:
            return True
        if "hidden" in values or str(values.get("aria-hidden", "")).casefold() == "true":
            return True
        style = re.sub(r"\s+", "", str(values.get("style", ""))).casefold()
        if "display:none" in style or "visibility:hidden" in style:
            return True
        tokens = (str(values.get("id", "")) + " " + str(values.get("class", ""))).split()
        return any(cls._NOISE_TOKEN.search(token) for token in tokens)

    @classmethod
    def _is_main_root(cls, tag, attrs):
        values = dict(attrs)
        if tag in {"main", "article"} or str(values.get("role", "")).casefold() == "main":
            return True
        return any(cls._MAIN_TOKEN.fullmatch(token) for token in
                   (str(values.get("id", "")) + " " + str(values.get("class", ""))).split())

    def handle_starttag(self, tag, attrs):
        if self._ignored_depth:
            if tag not in self._VOID_TAGS:
                self._ignored_depth += 1
            return
        if self._is_noise(tag, attrs, bool(self._main_roots)):
            if tag not in self._VOID_TAGS:
                self._ignored_depth = 1
            return
        if self._is_main_root(tag, attrs):
            self._main_roots.append(tag)
            self.has_main_content = True
        if tag in {"td", "th"}:
            self._append(" | ")
        elif tag in self._BLOCK_TAGS:
            self._boundary()
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.heading, self.heading_parts = int(tag[1]), []

    def handle_data(self, data):
        if self._ignored_depth or not data.strip():
            return
        if self.heading:
            self.heading_parts.append(data)
        else:
            self._append(data)

    def handle_endtag(self, tag):
        if self._ignored_depth:
            if tag in {"body", "html"}:
                self._ignored_depth = 0
            elif tag not in self._VOID_TAGS:
                self._ignored_depth -= 1
            return
        if self.heading and tag == "h" + str(self.heading):
            self._append("#" * self.heading + " " + " ".join(self.heading_parts).strip())
            self.heading = None
        if tag in self._BLOCK_TAGS and tag not in {"td", "th"}:
            self._boundary()
        for index in range(len(self._main_roots) - 1, -1, -1):
            if self._main_roots[index] == tag:
                del self._main_roots[index:]
                break

    @staticmethod
    def _normalise(parts):
        text = "".join(parts).replace("\xa0", " ").replace("\u200b", "")
        text = re.sub(r"[\t\r\f\v ]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def get_text(self):
        main_text = self._normalise(self.main_parts)
        return main_text if self.has_main_content and main_text else self._normalise(self.parts)


def _decode_html(body: bytes, content_type: str = "") -> str:
    """Decode declared HTML charsets first, then use safe common fallbacks."""
    if body.startswith(b"\xef\xbb\xbf"):
        return body.decode("utf-8-sig", errors="replace")
    charset = re.search(r"charset\s*=\s*[\"']?\s*([\w.-]+)", content_type, re.I)
    meta = re.search(br"<meta[^>]+charset\s*=\s*[\"']?\s*([\w.-]+)", body[:8192], re.I)
    declared = charset.group(1) if charset else meta.group(1).decode("ascii", "ignore") if meta else None
    candidates = [declared, "utf-8"]
    for encoding in candidates:
        if not encoding:
            continue
        try:
            return body.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    try:
        from charset_normalizer import from_bytes
        detected = from_bytes(body).best()
        if detected is not None:
            return str(detected)
    except ImportError:
        pass
    return body.decode("cp1252", errors="replace")


class TavilyMCP:
    def __init__(self, *, search_limit=2, page_limit=5):
        self.search_limit, self.page_limit = search_limit, page_limit
        self.search_calls = self.page_calls = 0
        self.request_timeout_seconds = 20.
        self.request_deadline = None
        self.session = None
        self.stack = AsyncExitStack()
        self.tools = {}
        self.on_progress = None
        self.registry = OfficialDomainRegistry()

    async def __aenter__(self):
        key = tavily_api_key() or ""
        if not key:
            raise RuntimeError("TAVILY_API_KEY is not configured")
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        try:
            read, write, _ = await self.stack.enter_async_context(streamablehttp_client(
                os.getenv("TAVILY_MCP_URL", "https://mcp.tavily.com/mcp/"),
                headers={"Authorization": "Bearer " + key}, timeout=20))
            self.session = await self.stack.enter_async_context(ClientSession(read, write))
            await self.session.initialize()
            offered = await self.session.list_tools()
            for tool in offered.tools:
                self.tools[tool.name.replace("-", "_")] = (tool.name, tool.inputSchema)
            if "tavily_search" not in self.tools:
                raise RuntimeError("MCP server did not offer tavily_search")
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def __aexit__(self, *args):
        return await self.stack.__aexit__(*args)

    async def _call(self, name, arguments):
        actual, schema = self.tools[name]
        supported = schema.get("properties", {})
        if any(k not in supported for k in arguments):
            raise ValueError("MCP schema does not support required scoped arguments")
        with span("mcp.search" if name == "tavily_search" else "mcp.extract", tool=name):
            # Use the MCP SDK's request timeout. Wrapping call_tool in asyncio.wait_for
            # cancels its AnyIO task group and can poison later calls in the session.
            timeout = max(.01, min(20, self.request_timeout_seconds))
            if self.request_deadline is not None:
                remaining = self.request_deadline - asyncio.get_running_loop().time()
                if remaining <= .02:
                    raise TimeoutError("MCP stage budget exhausted")
                timeout = min(timeout, remaining - .01)
            response = await self.session.call_tool(actual, arguments,
                read_timeout_seconds=timedelta(seconds=timeout))
        if response.isError:
            raise RuntimeError("MCP tool returned an error")
        if getattr(response, "structuredContent", None):
            return response.structuredContent
        text = "\n".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        return json.loads(text)

    async def search(self, query, domains=()):
        if self.search_calls >= self.search_limit:
            raise RuntimeError("Research search budget exhausted")
        self.search_calls += 1
        return await self._call("tavily_search", {"query": query, "include_domains": list(domains),
                                                  "max_results": 5, "search_depth": "basic"})

    async def read(self, url, domains):
        try:
            return await self._read_direct(url, domains)
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            # Never bypass HTTPS/domain/SSRF/content-size/redirect validation.
            # Certificate failures also remain fatal rather than being hidden.
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                raise
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in {403, 429, 502, 503, 504}:
                raise
            if os.getenv("RESEARCH_READ_FALLBACK", "1") != "1" or "tavily_extract" not in self.tools:
                raise
            if self.on_progress:
                await self.on_progress("read_fallback")
            # The original attempt already consumed this page allowance.
            return await self.extract(url, domains, consume_page=False)

    async def validate_url(self, url, domains):
        from .repair import ToolFailure
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").casefold()
            port = parsed.port
        except ValueError:
            raise ToolFailure("INVALID_URL") from None
        if parsed.username or parsed.password:
            raise ToolFailure("URL_CREDENTIALS_REJECTED")
        if not any(host == d or host.endswith("." + d) for d in domains):
            raise ToolFailure("OFFICIAL_DOMAIN_REJECTED")
        if parsed.scheme != "https":
            raise ToolFailure("HTTPS_REQUIRED")
        if port not in {None, 443}:
            raise ToolFailure("PORT_REJECTED")
        try:
            await asyncio.to_thread(_assert_public_host, host)
        except ValueError:
            raise ToolFailure("PRIVATE_ADDRESS_REJECTED") from None
        return {"valid": True}

    async def _read_direct(self, url, domains, *, precise_errors=False, allow_body_fallback=True):
        if self.page_calls >= self.page_limit:
            raise RuntimeError("Research page budget exhausted")
        self.page_calls += 1
        # Validate every redirect ourselves; search snippets never become evidence.
        proxy = os.getenv("RESEARCH_WEB_PROXY") or None
        async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=6), proxy=proxy,
                follow_redirects=False, headers={"User-Agent": "Mozilla/5.0 (compatible; OpportunityResearch/2.2)"}) as client:
            current = url
            for _ in range(6):
                parsed = urlparse(current)
                host = (parsed.hostname or "").casefold()
                if precise_errors:
                    await self.validate_url(current, domains)
                if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443} or not any(host == d or host.endswith("." + d) for d in domains):
                    raise ValueError("Page left verified official domains")
                await asyncio.to_thread(_assert_public_host, host)
                async with client.stream("GET", current) as response:
                    if response.is_redirect:
                        current = str(response.url.join(response.headers["location"]))
                        continue
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 2 * 1024 * 1024:
                            raise ValueError("Official page exceeds 2 MiB")
                    html = _decode_html(bytes(body), response.headers.get("content-type", ""))
                    parser = ResearchTextParser()
                    parser.feed(html)
                    text = parser.get_text()
                    # Only after validating final URL; never fall back around a validation failure.
                    if (allow_body_fallback and len(text.strip()) < 100 and not (parser.has_main_content and text.strip())
                            and "tavily_extract" in self.tools):
                        data = await self._call("tavily_extract", {"urls": [current], "format": "text", "extract_depth": "basic"})
                        for item in data.get("results", []):
                            if item.get("url") == current:
                                text = item.get("raw_content", "")[:200000]
                    return {"url": current, "title": _page_title(html), "text": text, "read_method": "direct"}
            raise ValueError("Too many official page redirects")

    async def extract(self, url, domains, *, consume_page=True, precise_errors=False):
        """MCP extraction fallback for official pages that reject a direct reader."""
        if "tavily_extract" not in self.tools:
            raise RuntimeError("MCP server did not offer tavily_extract")
        if consume_page and self.page_calls >= self.page_limit:
            raise RuntimeError("Research page budget exhausted")
        parsed = urlparse(url)
        if precise_errors:
            await self.validate_url(url, domains)
        host = (parsed.hostname or "").casefold()
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}
                or not any(host == domain or host.endswith("." + domain) for domain in domains)):
            raise ValueError("Extract URL left verified official domains")
        await asyncio.to_thread(_assert_public_host, host)
        if consume_page:
            self.page_calls += 1
        data = await self._call("tavily_extract", {"urls": [url], "format": "text", "extract_depth": "basic"})
        for item in data.get("results", []):
            returned = item.get("url", "")
            if precise_errors:
                await self.validate_url(returned, domains)
            returned_parsed = urlparse(returned)
            returned_host = (returned_parsed.hostname or "").casefold()
            if (returned_parsed.scheme != "https" or returned_parsed.username or returned_parsed.password
                    or returned_parsed.port not in {None, 443}
                    or not any(returned_host == domain or returned_host.endswith("." + domain) for domain in domains)):
                continue
            await asyncio.to_thread(_assert_public_host, returned_host)
            text = str(item.get("raw_content", ""))[:200000]
            if text.strip():
                return {"url": returned, "title": str(item.get("title", "")), "text": text, "read_method": "mcp_extract"}
        raise RuntimeError("MCP extract returned no readable official content")
