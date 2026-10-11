"""Import collected knowledge from registered official university pages.

Only knowledge tables are touched. The input SQLite database is opened read-only;
annotations, cases, users and memory are never copied. Import is additive and
idempotent; official-domain content is promoted by deterministic source checks
and does not wait for a separate human-review flag.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select

from ...official_research import OfficialDomainRegistry, classify_program_page
from ..db.models import OfficialSource, ResearchProgram, ResearchRequirement, KnowledgeDocument, KnowledgeChunk
from .normalization import supported_value
from .task import intake_supported


def load_corpus(directory):
    directory = Path(directory).resolve()
    draft = json.loads((directory / "draft.json").read_text(encoding="utf-8"))
    if draft.get("synthetic") is not False:
        raise ValueError("Only explicitly non-synthetic collected corpora may be imported")
    with sqlite3.connect((directory / "pool-clean.db").as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        tables = {table: [dict(row) for row in connection.execute("SELECT * FROM " + table)]
                  for table in ("official_sources", "knowledge_documents", "knowledge_chunks")}
    return draft, tables


async def import_corpus(session, directory):
    draft, tables = load_corpus(directory)
    report = {"programs_added": 0, "sources_added": 0, "documents_added": 0,
              "chunks_added": 0, "facts_added": 0, "verified_facts_added": 0,
              "unreviewed_facts_added": 0, "source_files_modified": False}
    programs, sources = {}, {}
    registry = OfficialDomainRegistry()
    for item in draft["programs"]:
        row = await session.scalar(select(ResearchProgram).where(
            ResearchProgram.university == item["university"], ResearchProgram.program == item["program"],
            ResearchProgram.intake == item["intake"]))
        if row is None:
            row = ResearchProgram(id=item["id"], university=item["university"], program=item["program"],
                                  intake=item["intake"], country=item.get("country", ""), aliases=item.get("aliases", []))
            session.add(row)
            await session.flush()
            report["programs_added"] += 1
        programs[item["id"]] = row
    for item in tables["official_sources"]:
        parsed = urlparse(item["url"])
        registered = registry.resolve(item["university"])
        host = (parsed.hostname or "").casefold()
        if not (registered and parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port in {None, 443} and any(host == d or host.endswith("." + d) for d in registered["domains"])):
            raise ValueError("Corpus contains an unregistered official source: " + item["id"])
        row = await session.scalar(select(OfficialSource).where(OfficialSource.url == item["url"]))
        if row is None:
            row = OfficialSource(**{k: item[k] for k in (
                "id", "source_key", "university", "program", "url", "title", "excerpt", "content_hash")},
                status="verified")
            session.add(row)
            await session.flush()
            report["sources_added"] += 1
        elif row.status != "verified":
            row.status = "verified"
        sources[item["id"]] = row
    documents = {}
    for item in tables["knowledge_documents"]:
        row = await session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.content_hash == item["content_hash"]))
        if row is None:
            meta = json.loads(item["metadata"])
            if meta.get("program_id") in programs:
                meta["program_id"] = programs[meta["program_id"]].id
            row = KnowledgeDocument(id=item["id"], source_id=sources[item["source_id"]].id,
                source_type=item["source_type"], authority=item["authority"], title=item["title"],
                url=item["url"], content_hash=item["content_hash"], metadata_json=meta)
            session.add(row)
            await session.flush()
            report["documents_added"] += 1
        documents[item["id"]] = row
    for item in tables["knowledge_chunks"]:
        document = documents[item["document_id"]]
        prior = await session.scalar(select(KnowledgeChunk).where(
            KnowledgeChunk.document_id == document.id, KnowledgeChunk.chunk_index == item["chunk_index"]))
        if prior:
            if prior.content != item["content"]:
                raise ValueError("Conflicting existing chunk: " + item["id"])
            continue
        embedding = json.loads(item["embedding"]) if item["embedding"] else None
        if embedding is not None and (len(embedding) != 384 or not all(isinstance(x, (int, float)) for x in embedding)):
            raise ValueError("Invalid corpus embedding")
        meta = json.loads(item["metadata"])
        if meta.get("program_id") in programs:
            meta["program_id"] = programs[meta["program_id"]].id
        session.add(KnowledgeChunk(id=item["id"], document_id=document.id, chunk_index=item["chunk_index"],
                                  content=item["content"], metadata_json=meta, embedding=embedding))
        report["chunks_added"] += 1
    await session.flush()
    # Treat collected official material as formal after deterministic checks:
    # registered HTTPS host, exact programme identity, explicit intake, and an
    # exact quote/value pair. A model or manual review flag is not required.
    by_url = {s.url: s for s in sources.values()}
    text_by_source = {}
    for document in draft["documents"]:
        text_by_source.setdefault(document["source_id"], []).append(document["text"])
    for item in draft["sources"]:
        source, program = by_url.get(item["url"]), programs.get(item["program_id"])
        if source is None or program is None:
            continue
        for fact in item.get("facts", []):
            if fact["field"] not in {"deadline", "gre_policy", "tuition", "language"}:
                continue
            raw_quote = fact["quote"]
            quote_present = any(raw_quote in text for text in text_by_source.get(item["id"], []))
            source_text = "\n".join(text_by_source.get(item["id"], []))
            match, _, _ = classify_program_page(program.program, item.get("title", ""), source.url, source_text)
            verified = (match == "exact" and intake_supported(program.intake, source_text)
                        and quote_present and supported_value(fact["field"], fact["value"], raw_quote))
            identifier = hashlib.sha256(("real150:" + program.id + ":" + source.id + ":" + fact["id"]).encode()).hexdigest()[:32]
            if await session.get(ResearchRequirement, identifier):
                continue
            verified_at = datetime.fromisoformat(item["retrieved_at"]).replace(tzinfo=timezone.utc)
            session.add(ResearchRequirement(id=identifier, program_id=program.id, source_id=source.id,
                field=fact["field"], value=fact["value"],
                qualifier=fact["value"] if fact["field"] == "gre_policy" else "",
                excerpt=raw_quote, content_hash=item["content_hash"], verified_at=verified_at,
                status="verified" if verified else "pending_review", program_match="exact" if verified else "pending_review"))
            report["facts_added"] += 1
            report["verified_facts_added" if verified else "unreviewed_facts_added"] += 1
    await session.flush()
    return report


async def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--commit", action="store_true", help="Without this flag, validate then roll back")
    args = parser.parse_args()
    from ..db.session import SessionLocal
    async with SessionLocal() as session:
        try:
            report = await import_corpus(session, args.directory)
            if args.commit:
                await session.commit()
            else:
                await session.rollback()
            print(json.dumps({**report, "committed": args.commit}, ensure_ascii=False))
        except BaseException:
            await session.rollback()
            raise


if __name__ == "__main__":
    asyncio.run(main())
