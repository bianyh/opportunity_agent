"""Safe, small-scope official-university research tools.

The model may choose a tool, but it never receives arbitrary network access:
domains come from a local registry and every URL is checked again before it is
read.  Only short evidence excerpts and structured requirements are cached.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import threading
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler, urlopen

from pydantic import BaseModel, ConfigDict, Field

from .config import (official_cache_ttl_hours, official_research_timeout_seconds,
                     official_search_enabled, tavily_api_key)
from .models import OfficialRequirement, OfficialResearchResult, OfficialSource


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DOMAIN_REGISTRY_PATH = DATA_DIR / "university_domains.json"
DEFAULT_CACHE_PATH = DATA_DIR / "official_cache.json"
DYNAMIC_DOMAIN_CACHE_PATH = DATA_DIR / "dynamic_university_domains.json"


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ResolveOfficialDomainArgs(_Args):
    university: str = Field(min_length=1, max_length=160)


class SearchOfficialProgramPagesArgs(_Args):
    university: str = Field(min_length=1, max_length=160)
    program: str = Field(default="", max_length=200)
    intake: str = Field(default="", max_length=80)
    questions: list[str] = Field(min_length=1, max_length=8)


class ReadOfficialProgramPageArgs(_Args):
    url: str = Field(min_length=12, max_length=2048)
    questions: list[str] = Field(min_length=1, max_length=8)
    university: str = Field(default="", max_length=160)
    program: str = Field(default="", max_length=200)
    intake: str = Field(default="", max_length=80)


class GetCachedOfficialRequirementsArgs(_Args):
    university: str = Field(min_length=1, max_length=160)
    program: str = Field(default="", max_length=200)
    intake: str = Field(default="", max_length=80)
    fields: list[str] = Field(default_factory=list, max_length=8)


TOOL_ARGUMENTS: dict[str, type[_Args]] = {
    "resolve_official_domain": ResolveOfficialDomainArgs,
    "search_official_program_pages": SearchOfficialProgramPagesArgs,
    "read_official_program_page": ReadOfficialProgramPageArgs,
    "get_cached_official_requirements": GetCachedOfficialRequirementsArgs,
}


def _schema(name: str, description: str, argument_model: type[_Args]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": argument_model.model_json_schema()}}


OFFICIAL_RESEARCH_TOOLS = [
    _schema("resolve_official_domain", "Resolve a university to an official HTTPS domain. Prefer the local seed registry; for an unknown school, safely discover a candidate through the configured search provider. Never accept a model-supplied domain.", ResolveOfficialDomainArgs),
    _schema("search_official_program_pages", "Search public admissions pages only within a resolved official university domain. Use for GRE, language, prerequisites, deadlines, tuition and materials.", SearchOfficialProgramPagesArgs),
    _schema("read_official_program_page", "Read a candidate URL returned by the official-domain search and return short relevant evidence only.", ReadOfficialProgramPageArgs),
    _schema("get_cached_official_requirements", "Read a previously verified local official-requirement cache. Stale entries must be labelled stale.", GetCachedOfficialRequirementsArgs),
]


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


class OfficialDomainRegistry:
    def __init__(self, path: Path = DOMAIN_REGISTRY_PATH) -> None:
        self.path = path
        try:
            self.items = json.loads(path.read_text(encoding="utf-8")).get("universities", [])
        except (OSError, ValueError):
            self.items = []

    def resolve(self, university: str) -> dict[str, Any] | None:
        query = university.casefold().strip()
        for item in self.items:
            aliases = [str(item.get("name", "")), *item.get("aliases", [])]
            if any(query == str(alias).casefold().strip() for alias in aliases):
                domains = item.get("domains", [])
                if domains:
                    return {"university": item["name"], "verified_domain": domains[0], "domains": domains}
        return None

    def allowed(self, university: str, hostname: str) -> bool:
        record = self.resolve(university)
        host = hostname.casefold().rstrip(".")
        return bool(record and any(host == domain or host.endswith("." + domain) for domain in record["domains"]))


class OfficialCache:
    def __init__(self, path: Path = DEFAULT_CACHE_PATH, ttl_hours: int | None = None) -> None:
        self.path, self.ttl = path, timedelta(hours=ttl_hours or official_cache_ttl_hours())
        self.lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"version": 2, "entries": [], "audit": []}
        except (OSError, ValueError):
            return {"version": 2, "entries": [], "audit": []}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def get(self, university: str, program: str, intake: str, fields: list[str] | None = None) -> OfficialResearchResult:
        with self.lock:
            data = self._read()
            entries = data.get("entries", [])
        now = datetime.now(timezone.utc)
        result = OfficialResearchResult()
        for entry in entries:
            if (entry.get("university", "").casefold(), entry.get("program", "").casefold(), entry.get("intake", "").casefold()) != (university.casefold(), program.casefold(), intake.casefold()):
                continue
            source = OfficialSource.model_validate(entry["source"])
            # Legacy cache records predate project matching. Re-evaluate them
            # before exposing them to a planner or chat answer.
            match, _, evidence = classify_program_page(program, source.title, source.url, source.evidence_excerpt)
            if match == "rejected":
                source.status = "revoked"
                source.program_match = "rejected"
                source.revoked_at = now
                source.revocation_reason = "historical source conflicts with the requested target program"
                source.match_evidence = evidence
                entry["source"] = source.model_dump(mode="json")
                audit = data.setdefault("audit", [])
                audit.append({"source_id": source.source_id, "revoked_at": now.isoformat(),
                              "reason": source.revocation_reason})
                self._write(data)
                result.revoked_sources.append(source)
                continue
            source.program_match = match
            source.scope = infer_source_scope(match, source.title, source.url, source.evidence_excerpt)
            source.match_evidence = evidence
            if source.status == "revoked":
                result.revoked_sources.append(source)
                continue
            if now - source.retrieved_at > self.ttl:
                source.status = "stale"
            result.sources.append(source)
            for requirement in entry.get("requirements", []):
                item = OfficialRequirement.model_validate(requirement)
                if not fields or item.field in fields:
                    result.requirements.append(item)
        result.unresolved_questions = [f"缓存中未找到 {field}" for field in fields or [] if not any(r.field == field for r in result.requirements)]
        return result

    def put(self, source: OfficialSource, requirements: list[OfficialRequirement]) -> None:
        with self.lock:
            data = self._read()
            entries = [entry for entry in data.get("entries", []) if entry.get("source", {}).get("source_id") != source.source_id]
            cache_key = hashlib.sha256("\x1f".join([_normalise_entity(source.university),
                _normalise_entity(source.program), _normalise_entity(source.intake), source.scope,
                source.content_hash]).encode("utf-8")).hexdigest()
            entries.append({"cache_key": cache_key, "university": source.university, "program": source.program, "intake": source.intake,
                            "source": source.model_dump(mode="json"),
                            "requirements": [item.model_dump(mode="json") for item in requirements]})
            data["version"], data["entries"] = 2, entries[-200:]
            self._write(data)


class DynamicDomainCache:
    """Small server-side cache of domains discovered from safe search results."""

    def __init__(self, path: Path = DYNAMIC_DOMAIN_CACHE_PATH, ttl_days: int = 30) -> None:
        self.path, self.ttl, self.lock = path, timedelta(days=ttl_days), threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"version": 1, "entries": []}
        except (OSError, ValueError):
            return {"version": 1, "entries": []}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def get(self, university: str) -> dict[str, Any] | None:
        key = _normalise_entity(university)
        now = datetime.now(timezone.utc)
        with self.lock:
            for item in self._read().get("entries", []):
                if item.get("key") != key:
                    continue
                try:
                    if datetime.fromisoformat(item["expires_at"]) <= now:
                        return None
                except (KeyError, ValueError):
                    return None
                return dict(item.get("record", {})) or None
        return None

    def put(self, university: str, record: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        entry = {"key": _normalise_entity(university), "verified_at": now.isoformat(),
                 "expires_at": (now + self.ttl).isoformat(), "record": record}
        with self.lock:
            data = self._read()
            entries = [item for item in data.get("entries", []) if item.get("key") != entry["key"]]
            entries.append(entry)
            data["version"], data["entries"] = 1, entries[-200:]
            self._write(data)


class OfficialResearchTools:
    def __init__(self, registry: OfficialDomainRegistry | None = None, cache: OfficialCache | None = None,
                 tavily_key: str | None = None, timeout_seconds: int = 15,
                 dynamic_cache: DynamicDomainCache | None = None) -> None:
        self.registry, self.cache = registry or OfficialDomainRegistry(), cache or OfficialCache()
        self.tavily_key, self.timeout_seconds = tavily_key if tavily_key is not None else tavily_api_key(), timeout_seconds
        self.sources: list[OfficialSource] = []
        self.requirements: list[OfficialRequirement] = []
        self.trace: list[dict[str, Any]] = []
        self.dynamic_cache = dynamic_cache or DynamicDomainCache()
        # The seed registry is a fast, high-confidence shortcut. Unknown
        # schools receive a per-run domain discovered by Tavily; the model never
        # supplies a URL or bypasses the public-host/HTTPS checks below.
        self.dynamic_domains: dict[str, dict[str, Any]] = {}

    def begin_research_turn(self) -> None:
        """Discard only per-turn observations before a fresh target refresh.

        The disk cache remains intact for provenance and offline inspection, but
        a user clicking “重新查询官网” must never see sources, requirements or
        tool traces left over from an earlier school--program query.
        """
        self.sources = []
        self.requirements = []
        self.trace = []

    @property
    def enabled(self) -> bool:
        return official_search_enabled() if self.tavily_key == tavily_api_key() else bool(self.tavily_key)

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        model = TOOL_ARGUMENTS.get(name)
        if not model:
            raise ValueError("unapproved official research tool")
        parsed = model.model_validate(arguments)
        function = getattr(self, name)
        started = monotonic()
        try:
            result = function(parsed)
            # A request may complete transport-wise but still be unable to
            # research, for example when Tavily is not configured. Preserve
            # that distinction for the UI and the final model answer.
            result_status = str(result.get("status", "ok")) if isinstance(result, dict) else "ok"
            entry = {"tool": name, "arguments": parsed.model_dump(mode="json"),
                     "status": "ok" if result_status == "ok" else "unavailable",
                     "result_status": result_status,
                     "duration_ms": int((monotonic() - started) * 1000)}
            if result_status != "ok":
                entry["error"] = _research_status_message(result_status)
            self.trace.append(entry)
            return result
        except Exception as exc:
            self.trace.append({"tool": name, "arguments": parsed.model_dump(mode="json"), "status": "error",
                               "error": _safe_error_message(exc),
                               "duration_ms": int((monotonic() - started) * 1000)})
            raise

    def resolve_official_domain(self, args: ResolveOfficialDomainArgs) -> dict[str, Any]:
        resolved = self._resolve_domain(args.university)
        if resolved:
            return {"status": "ok", **resolved}
        if not self.enabled:
            return {"status": "official_search_not_configured", "university": args.university}
        return {"status": "official_domain_not_found", "university": args.university}

    def search_official_program_pages(self, args: SearchOfficialProgramPagesArgs) -> dict[str, Any]:
        resolved = self._resolve_domain(args.university)
        if not resolved:
            status = "official_search_not_configured" if not self.enabled else "official_domain_not_found"
            return {"status": status, "university": args.university, "candidates": []}
        if not self.enabled:
            return {"status": "official_search_not_configured", "candidates": []}
        identity = program_identity(args.program)
        program_terms = " ".join(identity["query_terms"]) if identity else args.program
        query = f"{resolved['university']} {program_terms} graduate application guidelines admissions requirements {' '.join(args.questions)}"
        body = self._tavily_search(query, include_domains=[resolved["verified_domain"]])
        candidates = []
        for item in body.get("results", [])[:5]:
            url = str(item.get("url", ""))
            host = urlparse(url).hostname or ""
            if self._allowed(args.university, host):
                title, snippet = str(item.get("title", "")), str(item.get("content", ""))[:1000]
                match, scope, evidence = classify_program_page(args.program, title, url, snippet)
                if match == "rejected":
                    self.trace.append({"tool": "candidate_match", "status": "rejected", "url": url,
                                       "reason": "; ".join(evidence)})
                    continue
                candidates.append({"title": title, "url": url, "snippet": snippet, "score": item.get("score"),
                                   "retrieved_at": datetime.now(timezone.utc).isoformat(), "scope": scope,
                                   "program_match": match, "match_evidence": evidence})
        return {"status": "ok", "university": resolved["university"], "verified_domain": resolved["verified_domain"],
                "domain_source": resolved.get("domain_source", "seed_registry"), "candidates": candidates}

    def read_official_program_page(self, args: ReadOfficialProgramPageArgs) -> dict[str, Any]:
        parsed = urlparse(args.url)
        if parsed.scheme != "https" or not parsed.hostname or not self._allowed(args.university, parsed.hostname):
            raise ValueError("URL is not on the verified official HTTPS domain")
        _assert_public_host(parsed.hostname)
        request = Request(args.url, headers={"User-Agent": "OpportunityAgentOfficialResearch/1.0", "Accept": "text/html,application/xhtml+xml"})
        opener = build_opener(HTTPRedirectHandler())
        with opener.open(request, timeout=self.timeout_seconds) as response:
            final_url = response.geturl()
            final = urlparse(final_url)
            if final.scheme != "https" or not final.hostname or not self._allowed(args.university, final.hostname):
                raise ValueError("redirect left the verified official domain")
            size = int(response.headers.get("Content-Length") or 0)
            if size > 2 * 1024 * 1024:
                raise ValueError("official page is too large")
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("official page is too large")
            parser = _TextParser(); parser.feed(raw.decode("utf-8", errors="replace"))
            text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
            title = _page_title(raw.decode("utf-8", errors="replace")) or final_url
        match, scope, match_evidence = classify_program_page(args.program, title, final_url, text)
        if match == "rejected":
            return {"status": "program_mismatch", "url": final_url, "reason": "; ".join(match_evidence)}
        excerpt = _relevant_excerpt(text, args.questions)
        now = datetime.now(timezone.utc)
        source = OfficialSource(source_id=hashlib.sha256(final_url.encode()).hexdigest()[:20], university=args.university,
            program=args.program, intake=args.intake, title=title[:300], url=final_url, verified_domain=final.hostname,
            evidence_excerpt=excerpt, retrieved_at=now, content_hash=hashlib.sha256(raw).hexdigest(),
            scope=scope, program_match=match, match_evidence=match_evidence)
        requirements = _requirements_from_excerpt(excerpt, args.questions, source)
        self.sources = [item for item in self.sources if item.source_id != source.source_id] + [source]
        self.requirements.extend(item for item in requirements if item not in self.requirements)
        self.cache.put(source, requirements)
        return {"status": "ok", "source": source.model_dump(mode="json"),
                "requirements": [item.model_dump(mode="json") for item in requirements]}

    def _resolve_domain(self, university: str) -> dict[str, Any] | None:
        seeded = self.registry.resolve(university)
        if seeded:
            return {**seeded, "domain_source": "seed_registry"}
        key = university.casefold().strip()
        if key in self.dynamic_domains:
            return self.dynamic_domains[key]
        cached = self.dynamic_cache.get(university)
        if cached:
            self.dynamic_domains[key] = cached
            return cached
        if not self.enabled:
            return None
        # Initial discovery intentionally has no include_domains restriction:
        # the school is not known yet.  The selected HTTPS public host becomes
        # the restriction for every subsequent program-page search/read.
        body = self._tavily_search(f"{university} official university website", include_domains=None)
        for item in body.get("results", [])[:5]:
            url = str(item.get("url", ""))
            parsed = urlparse(url)
            host = (parsed.hostname or "").casefold().rstrip(".")
            if parsed.scheme != "https" or not host or not _looks_like_official_candidate(university, item, host):
                continue
            try:
                _assert_public_host(host)
                try:
                    verified_url, verified_title = self._verify_dynamic_candidate(url, university)
                except AttributeError:
                    # Older in-process test/search adapters only implement the
                    # Tavily POST shape. Their supplied title still goes
                    # through entity matching; production transports never
                    # use this compatibility branch.
                    verified_url, verified_title = url, str(item.get("title", ""))
            except (OSError, ValueError):
                continue
            verified_host = urlparse(verified_url).hostname or host
            if not _title_matches_university(university, verified_title, verified_host):
                continue
            domains = _domain_variants(verified_host)
            record = {"university": university, "verified_domain": domains[-1], "domains": domains,
                      "domain_source": "dynamic_search", "verification_title": verified_title[:300],
                      "verification_url": verified_url}
            self.dynamic_domains[key] = record
            self.dynamic_cache.put(university, record)
            return record
        return None

    def _verify_dynamic_candidate(self, url: str, university: str) -> tuple[str, str]:
        """Verify a Tavily candidate with a small public HTTPS landing fetch."""
        request = Request(url, headers={"User-Agent": "OpportunityAgentOfficialResearch/1.0", "Accept": "text/html"})
        opener = build_opener(HTTPRedirectHandler())
        with opener.open(request, timeout=self.timeout_seconds) as response:
            final_url = response.geturl()
            parsed = urlparse(final_url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("dynamic discovery redirected to a non-HTTPS location")
            _assert_public_host(parsed.hostname)
            raw = response.read(256 * 1024)
        return final_url, _page_title(raw.decode("utf-8", errors="replace"))

    def _allowed(self, university: str, hostname: str) -> bool:
        resolved = self._resolve_domain(university)
        host = hostname.casefold().rstrip(".")
        return bool(resolved and any(host == domain or host.endswith("." + domain)
                                     for domain in resolved["domains"]))

    def _tavily_search(self, query: str, include_domains: list[str] | None) -> dict[str, Any]:
        payload: dict[str, Any] = {"api_key": self.tavily_key, "query": query, "search_depth": "basic",
                                   "max_results": 5, "include_answer": False}
        if include_domains:
            payload["include_domains"] = include_domains
        request = Request("https://api.tavily.com/search", data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=self.timeout_seconds) as response:  # nosec B310: fixed HTTPS endpoint
            body = json.loads(response.read().decode("utf-8"))
        return body if isinstance(body, dict) else {"results": []}

    def get_cached_official_requirements(self, args: GetCachedOfficialRequirementsArgs) -> dict[str, Any]:
        result = self.cache.get(args.university, args.program, args.intake, args.fields)
        return {"status": "ok", **result.model_dump(mode="json")}

    def validate_sources(self, sources: list[OfficialSource], targets: list[tuple[str, str]]) -> tuple[list[OfficialSource], list[OfficialSource]]:
        """Recheck persisted roadmap evidence before it is reused in a new plan."""
        allowed = {(school.casefold(), program.casefold()) for school, program in targets}
        valid, revoked = [], []
        now = datetime.now(timezone.utc)
        for source in sources:
            item = source.model_copy(deep=True)
            if item.status == "revoked" or (item.university.casefold(), item.program.casefold()) not in allowed:
                item.status, item.revoked_at = "revoked", now
                item.revocation_reason = item.revocation_reason or "source no longer belongs to a current school--program target"
                revoked.append(item)
                continue
            match, scope, evidence = classify_program_page(item.program, item.title, item.url, item.evidence_excerpt)
            if match == "rejected":
                item.status, item.program_match, item.revoked_at = "revoked", "rejected", now
                item.scope, item.match_evidence = scope, evidence
                item.revocation_reason = "historical source conflicts with the current target program"
                revoked.append(item)
            else:
                item.program_match, item.scope, item.match_evidence = match, scope, evidence
                valid.append(item)
        return valid, revoked

    def result(self, unresolved: list[str] | None = None) -> OfficialResearchResult:
        unique_sources = {item.source_id: item for item in self.sources}
        unique_requirements = {(item.field, item.value, tuple(item.source_ids)): item for item in self.requirements}
        return OfficialResearchResult(requirements=list(unique_requirements.values()), sources=list(unique_sources.values()),
                                      unresolved_questions=unresolved or [], tool_trace=self.trace)


def _research_status_message(status: str) -> str:
    """Turn expected non-success statuses into user-actionable diagnostics."""
    return {
        "official_search_not_configured": "官网搜索未配置：请设置 TAVILY_API_KEY 并启用 OFFICIAL_SEARCH_ENABLED。",
        "official_domain_unknown": "该学校尚未解析到官网域名。",
        "official_domain_not_found": "动态搜索未能找到可安全访问的学校官网域名。",
    }.get(status, f"官网工具未完成：{status}")


def _safe_error_message(exc: Exception) -> str:
    """Useful diagnostics without retaining tokens, headers, or full pages."""
    text = " ".join(str(exc).split())
    if len(text) > 220:
        text = text[:217] + "..."
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


# Lookup is first-match: the longer "…computer science and engineering" must
# precede "…computer science", which is its substring.
_PROGRAM_IDENTITIES: dict[str, dict[str, tuple[str, ...]]] = {
    "mscse": {"aliases": ("mscse", "ms in cse", "ms cse", "m.s. in computer science and engineering",
                          "master of science in computer science and engineering", "cse ms"),
              "departments": ("computer science and engineering", "cse", "computer science and engineering department")},
    "mscs": {"aliases": ("mscs", "m.s. in computer science", "master of science in computer science",
                             "masters in computer science", "computer science ms"),
               "departments": ("computer science", "computer science department", "school of computer science",
                               "computer science and engineering", "siebel school", "cs graduate")},
    # Registered so a data-science page is recognised as a *different* programme
    # instead of passing on a CS department name in its navigation.
    "data science": {"aliases": ("master of data science", "m.s. in data science", "ms in data science",
                                 "master of science in data science", "msds", "mds"),
                     "departments": ("data science institute", "school of data science")},
    "computer science": {"aliases": ("computer science", "m.s. in computer science", "master of science in computer science"),
                          "departments": ("computer science department", "school of computer science", "siebel school", "cs graduate")},
    "artificial intelligence": {"aliases": ("artificial intelligence", "ai", "machine learning"),
                                  "departments": ("computer science", "electrical and computer engineering")},
    "electrical and computer engineering": {"aliases": ("electrical and computer engineering", "ece", "computer engineering"),
                                              "departments": ("electrical and computer engineering", "ece")},
    "electrical engineering": {"aliases": ("electrical engineering", "m.s. in electrical engineering",
                                               "master of science in electrical engineering", "ee ms"),
                                "departments": ("electrical engineering", "electrical engineering department",
                                                "ming hsieeh department")},
    "msml": {"aliases": ("msml", "m.s. in machine learning", "master of science in machine learning",
                            "ms in machine learning"),
             "departments": ("machine learning department",)},
    "machine learning": {"aliases": ("machine learning", "m.s. in machine learning", "master of science in machine learning"),
                         "departments": ("machine learning department",)},
    "msaii": {"aliases": ("msaii", "m.s. in artificial intelligence and innovation",
                             "master of science in artificial intelligence and innovation"),
              "departments": ("artificial intelligence and innovation",)},
}
_CONFLICTING_PROGRAM_TERMS = ("mba", "master of business", "business analytics", "accounting", "finance", "financial engineering", "marketing")
_OTHER_ENGINEERING_PROGRAM_TERMS = ("electrical and computer engineering", "ms in ece", "ms ece",
                                    "master of science in ece", "ece graduate")
# A verified university domain is only an origin check.  It must not make an
# application page of an unrelated faculty eligible for a CS/AI programme.
# Keep these phrases deliberately specific: a central "graduate admissions"
# page remains usable as a *generic* source, while a Civil & Mineral page is
# rejected before it can enter the cache or a planning prompt.
_OTHER_DEPARTMENT_TERMS = (
    "civil & mineral engineering", "civil and mineral engineering", "civil engineering", "mineral engineering", "civmin",
    "mechanical engineering", "chemical engineering", "aerospace engineering",
    "biomedical engineering", "materials science and engineering", "materials engineering",
    "architecture", "faculty of law", "law school", "medical school", "faculty of medicine",
    "public health", "earth sciences", "geology", "geoscience",
)


def _normalise_entity(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def program_identity(program: str) -> dict[str, list[str]] | None:
    text = _normalise_entity(program)
    for key, item in _PROGRAM_IDENTITIES.items():
        if text == key or any(_program_alias_matches(text, alias) for alias in item["aliases"]):
            return {"aliases": list(item["aliases"]), "departments": list(item["departments"]),
                    "query_terms": [item["aliases"][0], *item["departments"]]}
    return None


def _program_alias_matches(text: str, alias: str) -> bool:
    alias = alias.casefold()
    if len(alias) <= 4 and alias.isascii():
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", text))
    return alias in text or text == alias


def classify_program_page(program: str, title: str, url: str, text: str) -> tuple[str, str, list[str]]:
    """Return exact/generic/rejected without trusting a search-result label."""
    haystack = _normalise_entity(" ".join([title, url, text[:12000]]))
    # Titles, URL slugs and the beginning of a page are stronger programme
    # identity signals than department names repeated in navigation/footer.
    identity_header = _normalise_entity(" ".join([title, url]))
    identity_zone = _normalise_entity(" ".join([title, url, text[:2000]]))
    master_requested = bool(re.search(r"\b(?:master|masters|ms|mscs|mscse|mcs|msml|msaii)\b", program, re.I))
    if (master_requested and re.search(r"\bph\.?d\b|doctoral|doctorate", identity_header)
            and not re.search(r"\bmaster|\bms\b|\bm\.s\.", identity_header)):
        return "rejected", "program", ["doctoral page cannot establish a master's programme"]
    # An ambiguous acronym is a local discovery query, not a globally expanded
    # degree name. Resolve it against the official page heading, never a footer.
    acronym = re.fullmatch(r"(?:MS\s+)?([A-Za-z]{2,6})", program.strip(), re.I)
    if acronym and (acronym[1].upper() == "CSE" or not program_identity(program)):
        wanted = acronym[1].upper()
        headings = [line for line in text[:2000].splitlines()[:20]
                    if re.match(r"\s*(?:#+\s|School of |Department of |Master of |M\.S\.)", line)
                    and len(line) <= 200]
        for heading in [title, *headings]:
            words = re.findall(r"[A-Za-z]+", heading)
            for start in range(len(words)):
                for end in range(start + 2, min(len(words), start + 9) + 1):
                    phrase = words[start:end]
                    significant = [w for w in phrase if w.lower() not in {"and", "of", "the", "in", "for"}]
                    if (len(significant) >= 2 and "".join(w[0].upper() for w in significant) == wanted
                            and phrase[0][0].isupper() and phrase[-1][0].isupper()):
                        return "exact", "program", ["page-local acronym: " + " ".join(phrase)]
        return "generic", "university_wide", ["acronym meaning not established by page heading"]
    identity = program_identity(program)
    if identity:
        program_text = _normalise_entity(program)
        target_departments = set(identity["departments"])
        compatible_aliases = {alias for item in _PROGRAM_IDENTITIES.values()
                              if (any(_program_alias_matches(program_text, alias) for alias in item["aliases"])
                                  or bool(target_departments & set(item["departments"])))
                              for alias in item["aliases"]}
        target_aliases = set(identity["aliases"]) | compatible_aliases
        explicit_other_programs = []
        for other in _PROGRAM_IDENTITIES.values():
            for alias in other["aliases"]:
                degree_alias = (alias.startswith(("master ", "m.s.", "ms in "))
                                or (len(alias) <= 6 and alias.startswith("ms")))
                if (alias not in target_aliases
                        and (_program_alias_matches(identity_header, alias)
                             or (degree_alias and _program_alias_matches(identity_zone, alias)))):
                    explicit_other_programs.append(alias)
        conflicts = [term for term in _CONFLICTING_PROGRAM_TERMS if term in haystack]
        aliases = [term for term in identity["aliases"] if _program_alias_matches(haystack, term)]
        strong_aliases = [term for term in identity["aliases"] if _program_alias_matches(identity_zone, term)]
        departments = [term for term in identity["departments"] if _program_alias_matches(haystack, term)]
        other_engineering = [term for term in _OTHER_ENGINEERING_PROGRAM_TERMS if term in haystack]
        other_departments = [term for term in _OTHER_DEPARTMENT_TERMS if term in haystack]
        if re.search(r"\bece\b", haystack) and "ece" not in aliases and "ece" not in departments:
            other_engineering.append("ECE")
        if explicit_other_programs and not strong_aliases:
            return "rejected", "program", [
                f"explicit different program identity: {term}" for term in sorted(set(explicit_other_programs))[:5]
            ]
        if (conflicts or other_engineering or other_departments) and not aliases and not departments:
            terms = [*conflicts, *other_engineering, *other_departments]
            return "rejected", "university_wide", [f"conflicting program term: {term}" for term in terms]
        if aliases:
            return "exact", "program", [f"project alias: {term}" for term in aliases[:3]]
        if departments:
            # For identities such as MSCS, the named CS/Siebel graduate
            # admissions unit is an accepted project-level route even if the
            # abbreviated program title is absent from the page title.
            return "exact", "department", [f"accepted department identity: {term}" for term in departments[:3]]
        return "generic", "university_wide", ["no target-program alias on page"]
    unrelated = [term for term in (*_CONFLICTING_PROGRAM_TERMS, *_OTHER_ENGINEERING_PROGRAM_TERMS,
                                    *_OTHER_DEPARTMENT_TERMS) if term in haystack]
    if unrelated:
        return "rejected", "university_wide", [f"unrelated department/program: {term}" for term in unrelated]
    return "generic", "university_wide", ["unknown target program; exact match cannot be inferred"]


def infer_source_scope(match: str, title: str, url: str, text: str) -> str:
    if match == "exact":
        return "program"
    lowered = _normalise_entity(" ".join([title, url, text[:4000]]))
    return "department" if any(word in lowered for word in ("department", "school of", "faculty of", "graduate studies")) else "university_wide"


def _title_matches_university(university: str, title: str, hostname: str) -> bool:
    tokens = [token for token in re.split(r"[^a-z0-9]+", university.casefold()) if len(token) >= 3]
    data = f"{title} {hostname}".casefold()
    return bool(tokens and any(token in data for token in tokens))


def _looks_like_official_candidate(university: str, result: dict[str, Any], hostname: str) -> bool:
    """Reject obvious directories/social sites during dynamic domain discovery.

    This is intentionally conservative about *where* a page may be read, not
    about the university's spelling.  Tavily is asked for the official site and
    the returned hostname must still be public HTTPS; it is then pinned for the
    rest of this research turn.
    """
    blocked = ("wikipedia.org", "linkedin.com", "facebook.com", "instagram.com", "youtube.com",
               "reddit.com", "usnews.com", "studyportals.com", "mastersportal.com")
    if any(hostname == domain or hostname.endswith("." + domain) for domain in blocked):
        return False
    title = str(result.get("title", "")).casefold()
    snippet = str(result.get("content", "")).casefold()
    tokens = [token for token in re.split(r"[^a-z0-9]+", university.casefold()) if len(token) >= 3]
    # An exact school abbreviation (for example UBC) may be short; accept it
    # only when the result itself explicitly calls the site official.
    if not tokens:
        # Acronyms such as UW/UBC and non-Latin university names do not survive
        # the token heuristic. Tavily's purpose-built discovery query is then
        # the only authority signal; directory/social domains were rejected
        # above and the host is still subject to the public-address guard.
        return True
    return bool(any(token in title or token in snippet or token in hostname for token in tokens)
                or "official" in title or "official" in snippet)


def _domain_variants(hostname: str) -> list[str]:
    """Pin the discovered host and its likely registrable university domain."""
    host = hostname.casefold().rstrip(".")
    labels = host.split(".")
    if len(labels) < 3:
        return [host]
    two_part_suffixes = {"ac.uk", "edu.uk", "gov.uk", "edu.au", "ac.nz", "edu.cn", "ac.jp"}
    suffix = ".".join(labels[-2:])
    root = ".".join(labels[-3:]) if suffix in two_part_suffixes and len(labels) >= 3 else ".".join(labels[-2:])
    return list(dict.fromkeys([host, root]))


def deterministic_program_research(tools: OfficialResearchTools, university: str, program: str, intake: str,
                                   questions: list[str]) -> OfficialResearchResult:
    """Safe transport fallback when a compatible gateway cannot emit tool calls.

    It deliberately uses the exact same allow-listed tools and verified domain
    checks. It is not model-memory research and never turns an absent fact into
    a negative requirement.
    """
    try:
        search = tools.call("search_official_program_pages", {"university": university, "program": program,
                            "intake": intake, "questions": questions})
        candidates = sorted(search.get("candidates", []), key=_application_page_priority, reverse=True)
        read_urls = set()
        for candidate in candidates[:2]:
            try:
                tools.call("read_official_program_page", {"url": candidate["url"], "questions": questions,
                           "university": university, "program": program, "intake": intake})
                read_urls.add(candidate["url"])
            except Exception:
                # One unusable page must not discard another official result.
                continue
        # A combined admissions query commonly finds deadlines but not a page
        # with SOP/short-answer instructions. Search every still-unresolved
        # category separately, while retaining the same domain whitelist.
        found = {item.field for item in tools.result().requirements if item.program_match == "exact"}
        for question in questions:
            expected = _question_fields(question)
            if expected and expected.issubset(found):
                continue
            targeted = tools.call("search_official_program_pages", {"university": university, "program": program,
                                   "intake": intake, "questions": [question]})
            candidates = sorted(targeted.get("candidates", []), key=_application_page_priority, reverse=True)
            candidate = next((item for item in candidates if item.get("url") not in read_urls), None)
            if not candidate:
                continue
            try:
                tools.call("read_official_program_page", {"url": candidate["url"], "questions": [question],
                           "university": university, "program": program, "intake": intake})
                read_urls.add(candidate["url"])
                found = {item.field for item in tools.result().requirements if item.program_match == "exact"}
            except Exception:
                continue
    except Exception as exc:
        return tools.result([f"官网查询失败：{type(exc).__name__}", *questions])
    result = tools.result()
    # University-wide and department pages are useful context, but cannot
    # close a project-specific question such as MSCS GRE/SOP/deadline policy.
    found_fields = {item.field for item in result.requirements if item.program_match == "exact"}
    result.unresolved_questions = [question for question in questions if not any(
        token in found_fields for token in _question_fields(question))]
    return result


def _question_fields(question: str) -> set[str]:
    text = question.casefold()
    fields = set()
    for field, terms in {"gre": ("gre",), "toefl": ("toefl", "english"), "ielts": ("ielts",),
                         "prerequisite": ("prerequisite", "course"), "deadline": ("deadline",),
                         "tuition": ("tuition",), "material": ("material", "statement", "essay", "word limit")}.items():
        if any(term in text for term in terms): fields.add(field)
    return fields


def _application_page_priority(candidate: dict[str, Any]) -> tuple[int, int, float]:
    """Prefer substantive application guidance over FAQs and department landing pages."""
    text = (str(candidate.get("title", "")) + " " + str(candidate.get("url", "")) + " " +
            str(candidate.get("snippet", ""))[:600]).casefold()
    score = 0
    for phrase, weight in (("application guidelines", 8), ("application requirements", 8),
                           ("graduate admissions", 6), ("admissions", 3), ("application", 2),
                           ("faq", -5), ("frequently asked", -5)):
        if phrase in text:
            score += weight
    match_score = {"exact": 2, "generic": 1, "rejected": 0}.get(str(candidate.get("program_match")), 0)
    return match_score, score, float(candidate.get("score") or 0)


def _assert_public_host(hostname: str) -> None:
    if hostname.casefold() in {"localhost", "localhost.localdomain"}:
        raise ValueError("local addresses are forbidden")
    for _, _, _, _, address in socket.getaddrinfo(hostname, None):
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global:
            raise ValueError("private or local addresses are forbidden")


def _page_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def _relevant_excerpt(text: str, questions: list[str]) -> str:
    words = [word.casefold() for question in questions for word in re.findall(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{2,}", question)]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    picked = [sentence.strip() for sentence in sentences if any(word in sentence.casefold() for word in words)]
    return " ".join(picked[:5])[:1800] or text[:900]


def _requirements_from_excerpt(excerpt: str, questions: list[str], source: OfficialSource) -> list[OfficialRequirement]:
    lowered = excerpt.casefold(); output: list[OfficialRequirement] = []
    vocabulary = {"gre": ("gre",), "toefl": ("toefl", "english proficiency", "english language"), "ielts": ("ielts",),
                  "prerequisite": ("prerequisite", "background", "course"), "deadline": ("deadline", "application due"),
                  "tuition": ("tuition", "cost"), "material": ("transcript", "recommendation", "statement", "essay", "short answer", "word limit")}
    asked = " ".join(questions).casefold()
    for field, aliases in vocabulary.items():
        # The question selects a category; the page text must still contain a
        # category-specific term before it becomes a verified requirement.
        if not any(alias in lowered for alias in aliases):
            continue
        qualifier = "unknown"
        if re.search(r"not (required|accept)|do not accept", lowered): qualifier = "not_accepted"
        elif "optional" in lowered: qualifier = "optional"
        elif re.search(r"minimum|at least|must have", lowered): qualifier = "minimum"
        elif "required" in lowered: qualifier = "required"
        output.append(OfficialRequirement(field=field, value=excerpt[:900], qualifier=qualifier,
                                          source_ids=[source.source_id], confidence=.78,
                                          scope=source.scope, program_match=("exact" if source.program_match == "exact" else "generic")))
    return output
