"""Idempotent verified observations shared by online research and ingestion."""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import select, func, text as sql_text

from ...official_research import OfficialDomainRegistry, DynamicDomainCache, classify_program_page
from ..db.models import OfficialSource, ResearchProgram, ResearchRequirement
from .identity import school_aliases, program_aliases, canonical_school, canonical_program, normalize_intake
from .normalization import supported_value
from .task import intake_supported


async def persist_verified_page(session, program, page, facts, *, require_explicit_intake=True):
    """Write in the caller's short transaction; never commit unrelated work."""
    university, name, intake = program.university, program.program, normalize_intake(program.intake)
    url, title, body = page["url"], page["title"], page["text"]
    parsed = urlparse(url)
    record = OfficialDomainRegistry().resolve(university) or DynamicDomainCache().get(university)
    host = (parsed.hostname or "").casefold()
    if not (record and parsed.scheme == "https" and not parsed.username and not parsed.password
            and parsed.port in {None, 443} and any(host == d or host.endswith("." + d) for d in record["domains"])):
        raise ValueError("Fact persistence requires a verified official HTTPS source")
    match, _, _ = classify_program_page(name, title, url, body)
    intake_is_explicit = intake_supported(intake, body)
    if match != "exact" or (require_explicit_intake and not intake_is_explicit):
        raise ValueError("Official page persistence requires an exact programme and the requested intake scope")
    if facts and not intake_is_explicit:
        raise ValueError("Structured facts require an intake explicitly supported by the source")
    for fact in facts:
        if (fact["field"] not in {"deadline", "gre_policy", "tuition", "language"}
                or not fact["quote"] or fact["quote"] not in body
                or not supported_value(fact["field"], fact["value"], fact["quote"])):
            raise ValueError("Fact does not match its source quote")

    # Programme locks cover aliases; URL locks also protect shared source rows.
    if session.bind.dialect.name == "postgresql":
        await session.execute(sql_text("SET LOCAL lock_timeout = '2s'"))
        await session.execute(sql_text("SET LOCAL statement_timeout = '4s'"))
        identities = ["program:" + "|".join((canonical_school(university), canonical_program(name), intake)), "source:" + url]
        keys = sorted(int.from_bytes(hashlib.sha256(k.encode()).digest()[:8], "big", signed=True) for k in identities)
        for key in keys:
            await session.execute(sql_text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    rows = list((await session.scalars(select(ResearchProgram).where(
        func.lower(ResearchProgram.university).in_([a.casefold() for a in school_aliases(university)]),
        func.lower(ResearchProgram.program).in_([a.casefold() for a in program_aliases(name)])
    ).order_by(ResearchProgram.id))).all())
    row = next((r for r in rows if normalize_intake(r.intake).casefold() == intake.casefold()), None)
    if row is None:
        row = ResearchProgram(university=school_aliases(university)[0], program=program_aliases(name)[0],
                              intake=intake, country=program.country or "")
        session.add(row)
        await session.flush()
    digest = hashlib.sha256(body.encode()).hexdigest()
    source = await session.scalar(select(OfficialSource).where(OfficialSource.url == url))
    if source is None:
        source = OfficialSource(source_key=hashlib.sha256(url.encode()).hexdigest(), university=row.university,
                                program=row.program, url=url, title=title, status="verified")
        session.add(source)
        await session.flush()
    source.content_hash, source.excerpt, source.title, source.status = digest, body[:1000], title, "verified"
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=30)
    written, refreshed = 0, 0
    grouped = {}
    for fact in facts:
        grouped.setdefault(fact["field"], {})[(fact["value"], fact["quote"])] = fact
    for field, values in grouped.items():
        observations = list((await session.scalars(select(ResearchRequirement).where(
            ResearchRequirement.program_id == row.id, ResearchRequirement.source_id == source.id,
            ResearchRequirement.field == field))).all())
        active = [o for o in observations if o.status == "verified" and o.program_match == "exact"]
        for old in active:
            if not any(old.content_hash == digest and old.value == f["value"] and old.excerpt == f["quote"] for f in values.values()):
                old.status = "superseded"
        for fact in values.values():
            prior = next((o for o in active if o.content_hash == digest and o.value == fact["value"] and o.excerpt == fact["quote"]), None)
            if prior:
                prior.verified_at, prior.expires_at = now, expires
                refreshed += 1
            else:
                session.add(ResearchRequirement(program_id=row.id, source_id=source.id, field=field,
                    value=fact["value"], date_value=date.fromisoformat(fact["value"]) if field == "deadline" else None,
                    qualifier=fact["value"] if field == "gre_policy" else fact.get("qualifier", ""),
                    excerpt=fact["quote"], content_hash=digest, verified_at=now, expires_at=expires,
                    status="verified", program_match="exact"))
                written += 1
    await session.flush()
    return {"program_id": row.id, "source": source, "written": written, "refreshed": refreshed}
