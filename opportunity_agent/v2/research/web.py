"""Bounded Tavily MCP transport and independently verified official-page reading."""
from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import re
from contextlib import AsyncExitStack
from datetime import timedelta
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from xml.etree import ElementTree

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


class _SearchResultParser(_TextParser):
    """Small HTML parser for public search-engine result pages.

    Search results are only a URL discovery mechanism.  They never become
    evidence: every returned URL is checked against the registered university
    domains and fetched again by :class:`OfficialWeb.read`.
    """

    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._parts = []

    def handle_data(self, data):
        if self._href and data.strip():
            self._parts.append(data.strip())

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            title = re.sub(r"\s+", " ", " ".join(self._parts)).strip()
            self.links.append((self._href, title))
            self._href, self._parts = "", []


class OfficialWeb:
    """Direct, bounded retrieval from registered official university domains.

    Tavily remains available as an optional accelerator, but the research path
    must also work when no third-party search key is configured.  This adapter
    discovers candidate URLs from robots/sitemaps and, when necessary, a
    public search result page.  Only URLs under the supplied registered
    domains are returned or read.
    """

    _DEFAULT_PATHS = (
        "/admissions", "/graduate/admissions", "/graduate/programs",
        "/programs", "/programs/admissions", "/apply", "/application",
        "/application-requirements", "/deadlines", "/admission",
    )

    def __init__(self, *, search_limit=3, page_limit=8, timeout_seconds=7):
        self.search_limit, self.page_limit = search_limit, page_limit
        self.search_calls = self.page_calls = 0
        self.timeout_seconds = max(3, int(timeout_seconds))
        self.request_timeout_seconds = float(self.timeout_seconds)
        self.request_deadline = None
        self.on_progress = None
        self.client: httpx.AsyncClient | None = None
        self.registry = OfficialDomainRegistry()
        self._sitemap_cache: dict[str, list[str]] = {}
        self._robots_rules: dict[str, list[tuple[str, str]]] = {}

    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds, connect=min(6, self.timeout_seconds)),
            follow_redirects=False,
            headers={"User-Agent": "OpportunityAgentOfficialResearch/2.0", "Accept": "text/html,application/xml,text/plain,*/*"},
        )
        return self

    async def __aexit__(self, *args):
        if self.client is not None:
            await self.client.aclose()
            self.client = None
        return False

    @staticmethod
    def _allowed(url, domains):
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        return bool(parsed.scheme == "https" and not parsed.username and not parsed.password
                    and parsed.port in {None, 443}
                    and any(host == d or host.endswith("." + d) for d in domains))

    async def _fetch(self, url, domains, *, max_bytes=2 * 1024 * 1024, soft=False):
        if self.client is None:
            raise RuntimeError("OfficialWeb must be used as an async context manager")
        current = url
        for _ in range(5):
            if not self._allowed(current, domains):
                raise ValueError("URL left verified official domains")
            host = (urlparse(current).hostname or "").casefold()
            await asyncio.to_thread(_assert_public_host, host)
            try:
                response = await self.client.get(current)
                if response.is_redirect:
                    current = urljoin(current, response.headers.get("location", ""))
                    continue
                response.raise_for_status()
                if int(response.headers.get("content-length") or 0) > max_bytes:
                    raise ValueError("Official response exceeds 2 MiB")
                body = await response.aread()
                if len(body) > max_bytes:
                    raise ValueError("Official response exceeds 2 MiB")
                return current, response.headers.get("content-type", ""), bytes(body)
            except Exception:
                if soft:
                    return None
                raise
        raise ValueError("Too many official page redirects")

    @staticmethod
    def _candidate_roots(domains, query=""):
        """Build a small set of likely official subdomains under each root."""
        lowered = query.casefold()
        labels = []
        if re.search(r"\b(?:mscs|mcs|cse|computer|cs)\b|计算机", lowered):
            labels = ["www.csd", "www.cs", "www.eecs", "www.ece"]
        if re.search(r"\b(?:msaii|ai|artificial|intelligence|machine|ml)\b|人工智能|机器学习", lowered):
            labels = ["msaii.cs", "www.lti", "www.cs", "ai", "ml", *labels]
        if not labels:
            labels = ["www.gradschool", "www.graduate", "www.admissions"]
        roots = []
        for domain in domains:
            # Prefer the canonical www host for redirects such as cs.cmu.edu
            # -> www.cs.cmu.edu; the alias can still be probed after it.
            roots.append(f"https://www.{domain}")
            roots.append(f"https://{domain}")
            roots.extend(f"https://{label}.{domain}" for label in labels)
        return list(dict.fromkeys(roots))[:12]

    async def _sitemap_urls(self, domains, query=""):
        """Read a few robots/sitemap documents without spending page budget."""
        cache_key = "|".join((*domains, query.casefold().strip()))
        if cache_key in self._sitemap_cache:
            return self._sitemap_cache[cache_key]
        probes = []
        from .program_directory import navigation_entries
        programme_roots = list(dict.fromkeys(
            f"https://{urlparse(url).netloc}" for entry in navigation_entries(query)
            for url in entry["urls"] if self._allowed(url, domains)))
        # A known programme site is a stronger starting point than guessing
        # graduate-school subdomains across the whole university.
        for root in (programme_roots or self._candidate_roots(domains, query)):
            domain = next((item for item in domains if urlparse(root).hostname == item
                           or urlparse(root).hostname.endswith("." + item)), domains[0])
            probes.append((domain, "robots", root + "/robots.txt"))
            if not programme_roots:
                probes.append((domain, "sitemap", root + "/sitemap.xml"))
            if urlparse(root).hostname not in {domain, "www." + domain}:
                probes.append((domain, "home", root + "/"))
        fetched_probes = await asyncio.gather(*(
            self._fetch(url, domains, max_bytes=512 * 1024 if kind == "robots" else 4 * 1024 * 1024, soft=True)
            for _, kind, url in probes), return_exceptions=True)
        sitemap_urls: list[str] = []
        linked_pages: list[str] = []
        for (domain, kind, _), fetched in zip(probes, fetched_probes, strict=True):
            if not fetched or isinstance(fetched, BaseException):
                continue
            if kind == "robots":
                text = _decode_html(fetched[2], fetched[1])
                sitemap_urls.extend(re.findall(r"(?im)^\s*sitemap:\s*(\S+)", text))
                self._robots_rules[domain] = self._parse_robots(text)
            elif kind == "sitemap":
                sitemap_urls.append(fetched[0])
            else:
                parser = _SearchResultParser()
                parser.feed(_decode_html(fetched[2], fetched[1]))
                for href, _ in parser.links:
                    candidate = urljoin(fetched[0], html.unescape(href))
                    if self._allowed(candidate, domains) and self._robots_allowed(candidate):
                        linked_pages.append(candidate)
        if programme_roots:
            # The directory already identifies the programme's application
            # page. Home-page links supply genuine alternatives if it moves;
            # university-wide sitemap crawling only delays the first read.
            result = list(dict.fromkeys(url for url in linked_pages if self._robots_allowed(url)))
            self._sitemap_cache[cache_key] = result
            return result
        if not sitemap_urls:
            sitemap_urls.extend(f"https://{domain}{path}" for domain in domains
                                for path in ("/sitemap_index.xml", "/sitemap-index.xml"))
        pending = list(dict.fromkeys(u for u in sitemap_urls if self._allowed(u, domains)))
        seen, pages = set(), []
        while pending and len(seen) < 4 and len(pages) < 250:
            batch = []
            while pending and len(batch) < 4 and len(seen) < 8:
                sitemap = pending.pop(0)
                if sitemap not in seen:
                    seen.add(sitemap)
                    batch.append(sitemap)
            fetched_batch = await asyncio.gather(*(
                self._fetch(sitemap, domains, max_bytes=4 * 1024 * 1024, soft=True)
                for sitemap in batch), return_exceptions=True)
            for fetched in fetched_batch:
                if not fetched or isinstance(fetched, BaseException):
                    continue
                try:
                    root = ElementTree.fromstring(fetched[2])
                except ElementTree.ParseError:
                    continue
                tag = root.tag.rsplit("}", 1)[-1].casefold()
                locs = [str(item.text or "").strip() for item in root.iter()
                        if item.tag.rsplit("}", 1)[-1].casefold() == "loc" and item.text]
                if tag == "sitemapindex":
                    pending.extend(item for item in locs if self._allowed(item, domains) and item not in seen)
                else:
                    pages.extend(item for item in locs if self._allowed(item, domains))
        # Follow a few high-scoring official HTML pages one hop. University
        # program pages are often hosted on a department subdomain and linked
        # from a central school page rather than listed in the root sitemap.
        deep_seeds = self._rank(linked_pages, query)[:2]
        deep_fetched = await asyncio.gather(*(
            self._fetch(url, domains, max_bytes=2 * 1024 * 1024, soft=True)
            for url in deep_seeds), return_exceptions=True)
        deep_links = []
        for fetched in deep_fetched:
            if not fetched or isinstance(fetched, BaseException):
                continue
            parser = _SearchResultParser()
            parser.feed(_decode_html(fetched[2], fetched[1]))
            for href, _ in parser.links:
                candidate = urljoin(fetched[0], html.unescape(href))
                if self._allowed(candidate, domains) and self._robots_allowed(candidate):
                    deep_links.append(candidate)
        result = list(dict.fromkeys([*pages, *linked_pages, *deep_links]))
        result = [url for url in result if self._robots_allowed(url)][:500]
        self._sitemap_cache[cache_key] = result
        return result

    @staticmethod
    def _parse_robots(text):
        groups, agents, rules = [], [], []
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key, value = key.casefold(), value.strip()
            if key == "user-agent":
                if rules:
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(value.casefold())
            elif key in {"allow", "disallow"} and agents and value:
                rules.append((key, value))
        if agents:
            groups.append((agents, rules))
        chosen = [rule for group_agents, group_rules in groups
                  if "*" in group_agents or any("opportunityagent" in agent for agent in group_agents)
                  for rule in group_rules]
        return chosen

    def _robots_allowed(self, url):
        host = (urlparse(url).hostname or "").casefold().rstrip(".")
        path = urlparse(url).path or "/"
        matching = [(domain, rules) for domain, rules in self._robots_rules.items()
                    if host == domain or host.endswith("." + domain)]
        if not matching:
            return True
        rules = max(matching, key=lambda item: len(item[0]))[1]
        applicable = [(kind, rule) for kind, rule in rules if path.startswith(rule)]
        if not applicable:
            return True
        kind, _ = max(applicable, key=lambda item: (len(item[1]), item[0] == "allow"))
        return kind != "disallow"

    @staticmethod
    def _query_terms(query):
        terms = set(re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]", query.casefold()))
        expansions = {
            "mscs": {"master", "science", "computer", "computer-science", "cs", "graduate"},
            "mcs": {"master", "science", "computer", "computer-science", "cs", "graduate"},
            "cse": {"computer", "science", "engineering", "graduate"},
            "msaii": {"master", "artificial", "intelligence", "innovation", "ai"},
            "msml": {"master", "machine", "learning", "graduate"},
        }
        for key, values in expansions.items():
            if key in terms:
                terms.update(values)
        return terms

    def _rank(self, urls, query):
        terms = self._query_terms(query)
        scored = []
        for url in dict.fromkeys(urls):
            parsed = urlparse(url)
            text = (unquote(parsed.netloc + " " + parsed.path).casefold()
                    .replace("-", " ").replace("_", " ").replace("/", " "))
            score = sum(2.0 for term in terms if term in text)
            score += sum(1.5 for marker in ("admission", "application", "deadline", "requirement", "program", "degree", "graduate") if marker in text)
            score += sum(3.0 for marker in ("education master", "education masters", "education graduate admissions", "apply", "requirements") if marker in text)
            score -= 2.0 * sum(1 for marker in ("news", "event", "calendar", "directory", "faculty", "people", "athletics") if marker in text)
            if any(marker in text for marker in ("cs.cmu", "computer-science", "computer science", "mscs", "mcs")):
                score += 4
            if parsed.netloc.casefold().startswith("www.cs.") and "graduate admissions" in text:
                score += 2
            if (parsed.netloc.casefold().startswith("www.cs.")
                    and parsed.path.casefold().rstrip("/") == "/education/graduate-admissions"):
                score += 10
            if "csd" in text and re.search(r"\b(?:mscs|mcs|cse|computer|cs)\b", query.casefold()):
                score += 8
            if "education master index" in text and re.search(r"\b(?:mscs|mcs|cse)\b", query.casefold()):
                score += 5
            if "lti" in text and re.search(r"\bmsaii\b|artificial intelligence", query.casefold()):
                score += 5
            if "msaii" in text and re.search(r"\bmsaii\b", query.casefold()):
                score += 12
            if any(marker in text for marker in ("ai.cmu", "msaii", "artificial-intelligence")):
                score += 4
            if parsed.netloc.casefold().startswith("www."):
                score += 1.5
            elif parsed.netloc.casefold().split(".", 1)[0] in {"cs", "eecs", "ece"}:
                score -= 1.5
            scored.append((score, len(text), url))
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        # Department aliases commonly redirect to the same canonical page.
        # Same paths on different department sites are NOT the same page.
        # Only collapse the common www/non-www alias of the same host.
        result, seen_paths = [], set()
        for _, _, url in scored:
            parsed = urlparse(url)
            key = (parsed.netloc.casefold().removeprefix("www."),
                   (parsed.path or "/").rstrip("/").casefold() or "/", parsed.query)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            result.append(url)
        return result

    async def _search_engine(self, query, domains):
        if self.client is None:
            return []
        site = " OR ".join("site:" + domain for domain in domains)
        search_query = f"{site} {query} admissions program deadline requirements"
        custom_endpoint = os.getenv("OFFICIAL_SEARCH_URL")
        if not custom_endpoint:
            return []
        endpoints = [custom_endpoint]
        for endpoint in endpoints:
            params = {"q": search_query, "num": "10", "hl": "en"}
            try:
                async with asyncio.timeout(3):
                    response = await self.client.get(endpoint, params=params, headers={"Accept": "text/html"})
                response.raise_for_status()
                parser = _SearchResultParser()
                parser.feed(response.text)
            except Exception:
                continue
            found = []
            for href, title in parser.links:
                # Google often wraps links in /url?q=...; never trust the
                # wrapper and validate the unwrapped URL against the registry.
                if href.startswith("/url?"):
                    href = parse_qs(urlparse(href).query).get("q", [""])[0]
                href = html.unescape(href)
                bing = urlparse(href)
                if bing.path.casefold() == "/ck/a":
                    encoded = parse_qs(bing.query).get("u", [""])[0]
                    if encoded.startswith("a1"):
                        try:
                            href = base64.urlsafe_b64decode(encoded[2:] + "=" * ((4 - len(encoded[2:]) % 4) % 4)).decode()
                        except (ValueError, UnicodeDecodeError):
                            href = ""
                if self._allowed(href, domains) and self._robots_allowed(href):
                    found.append((href, title))
            if found:
                return found
        return []

    async def model_search(self, query, domains):
        """Real search results for model selection; never admissions evidence.

        No provider key is sent to public search. Model-written queries are
        preserved; official-host filtering is the only semantic-independent gate.
        """
        if self.client is None:
            return []
        found = []
        for search_query in (query, query + " " + " ".join("site:" + d for d in domains)):
            try:
                response = await self.client.get("https://www.bing.com/search", params={
                    "q": search_query, "format": "rss", "setlang": "en-US", "cc": "US"},
                    headers={"Accept-Language":"en-US,en;q=0.9"}, follow_redirects=True)
                response.raise_for_status()
                root = ElementTree.fromstring(response.content)
                for item in root.findall(".//item"):
                    url = item.findtext("link") or ""
                    if self._allowed(url, domains):
                        found.append({"url": url, "title": item.findtext("title") or "",
                                      "content": item.findtext("description") or ""})
            except Exception:
                continue
            if found:
                break
        if not found:
            # Bing may return an HTML result page after RSS throttling. Parse
            # only its direct HTTPS links; host validation still happens below.
            try:
                response = await self.client.get("https://www.bing.com/search", params={"q": query},
                    headers={"Accept-Language":"en-US,en;q=0.9"}, follow_redirects=True)
                parser = _SearchResultParser(); parser.feed(response.text)
                for href, title in parser.links:
                    candidate = urljoin(str(response.url), html.unescape(href)).split("#",1)[0]
                    if self._allowed(candidate, domains):
                        found.append({"url":candidate,"title":title,"content":"search navigation"})
            except Exception:
                pass
        return list({r["url"]: r for r in found}.values())[:8]

    async def search(self, query, domains=()):
        if self.search_calls >= self.search_limit:
            raise RuntimeError("Research search budget exhausted")
        self.search_calls += 1
        domains = tuple(str(d).casefold().rstrip(".") for d in domains)
        if not domains:
            return {"results": [], "diagnostic": "no_registered_domain"}
        if getattr(self, "model_directed", False):
            results = await self.model_search(query, domains)
            custom = await self._search_engine(query, domains) if os.getenv("OFFICIAL_SEARCH_URL") else []
            results.extend({"url": u, "title": t, "content": "search navigation"} for u, t in custom)
            return {"results": results, "diagnostic": {"provider": "bing_rss", "search_candidates": len(results)}}
        from .program_directory import navigation_entries
        entries = list(navigation_entries(query))
        seeds = [url for entry in entries for url in entry["urls"] if self._allowed(url, domains)]
        query = " ".join([query, *(entry["abbreviation"] for entry in entries)])
        sitemap_pages = await self._sitemap_urls(domains, query)
        search_pages = await self._search_engine(query, domains)
        discovered = [item[0] for item in search_pages] + sitemap_pages
        urls = []
        paths = list(self._DEFAULT_PATHS) + [
            "/education/graduate-admissions",
            "/education/graduate-studies/admissions",
            "/graduate-admissions", "/graduate/admissions",
        ]
        if re.search(r"\bmsaii\b", query.casefold()):
            paths.extend(["/apply-msaii-program", "/about-msaii-program", "/curriculum"])
        if re.search(r"\b(?:mscs|mcs|cse)\b", query.casefold()):
            paths.extend(["/education/master/index.html", "/education/masters/index.html",
                          "/academics/masters/ms-in-computer-science"])
        if re.search(r"\bmsaii\b", query.casefold()):
            paths.extend(["/academics/masters-programs/msaii.html"])
        for root in self._candidate_roots(domains, query):
            urls.extend(urljoin(root + "/", path.lstrip("/")) for path in paths)
        # Source-linked URLs and verified navigation seeds take precedence over
        # speculative paths. A guessed 404 must not evict a real project page.
        ranked = list(dict.fromkeys([*seeds, *self._rank(discovered, query), *self._rank(urls, query)]))
        ranked = [url for url in ranked if self._allowed(url, domains) and self._robots_allowed(url)]
        title_by_url = {url: title for url, title in search_pages}
        return {"results": [{"url": url, "title": title_by_url.get(url, ""), "content": "official URL candidate"}
                             for url in ranked[:max(5, min(12, self.page_limit * 2))]],
                "diagnostic": {"sitemap_candidates": len(sitemap_pages), "search_candidates": len(search_pages),
                               "directory_candidates": len(seeds)}}

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
        if precise_errors:
            await self.validate_url(url, domains)
        if getattr(self, "model_directed", False):
            host = (urlparse(url).hostname or "").casefold()
            if host not in self._robots_rules:
                robots = await self._fetch(f"https://{host}/robots.txt", domains, max_bytes=512*1024, soft=True)
                self._robots_rules[host] = self._parse_robots(_decode_html(robots[2], robots[1])) if robots else []
        if not self._robots_allowed(url):
            raise ValueError("robots.txt disallows this official URL")
        self.page_calls += 1
        final_url, content_type, body = await self._fetch(url, domains)
        if "pdf" in content_type.casefold() or final_url.casefold().endswith(".pdf"):
            raise ValueError("PDF official pages are not supported by the HTML reader")
        html_text = _decode_html(body, content_type)
        parser = ResearchTextParser()
        parser.feed(html_text)
        text = parser.get_text()
        if not text.strip():
            text = re.sub(r"\s+", " ", html.unescape(html_text)).strip()
        if len(text.strip()) < 80:
            raise ValueError("Official page has no readable text")
        link_parser = _SearchResultParser()
        link_parser.feed(html_text)
        links = []
        seen_links = set()
        for href, label in link_parser.links:
            candidate = urljoin(final_url, html.unescape(href)).split("#", 1)[0]
            if candidate not in seen_links and self._allowed(candidate, domains):
                seen_links.add(candidate)
                links.append({"url": candidate, "title": label[:200]})
        return {"url": final_url, "title": _page_title(html_text) or final_url,
                "text": text[:200000], "links": links[:120], "read_method": "direct_official"}

    async def read(self, url, domains):
        return await self._read_direct(url, domains)

    async def extract(self, url, domains, *, consume_page=True, precise_errors=False):
        if not consume_page:
            self.page_calls = max(0, self.page_calls - 1)
        return await self._read_direct(url, domains, precise_errors=precise_errors)
