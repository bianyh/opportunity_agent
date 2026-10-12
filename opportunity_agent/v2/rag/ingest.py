from __future__ import annotations

import hashlib
import asyncio
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import KnowledgeChunk, KnowledgeDocument, OfficialSource
from .models import shared_embedder
from .retrieval import lexical_text


def chunk_text(text: str, size: int = 350, overlap: int = 50, tokenizer=None) -> list[str]:
    if not 0 <= overlap < size:
        raise ValueError("Require 0 <= overlap < size")
    if not text.strip():
        return []
    if tokenizer is not None:
        offsets = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)["offset_mapping"]
    else:
        offsets = [m.span() for m in re.finditer(r"[\u4e00-\u9fff]|\w+|[^\w\s]", text)]
    chunks, start = [], 0
    while start < len(offsets):
        end = min(start + size, len(offsets))
        if end < len(offsets):
            # Prefer a sentence boundary in the latter half of the window.
            for index in range(end - 1, start + size // 2, -1):
                if re.search(r"[。！？.!?]\s*$|\n\s*$", text[offsets[index][0]:offsets[index + 1][0]]):
                    end = index + 1
                    break
        chunks.append(text[offsets[start][0]:offsets[end - 1][1]])
        if end == len(offsets):
            break
        start = max(start + 1, end - overlap)
    return chunks


def chunk_sectioned_text(text: str, *, tokenizer=None, size: int = 350,
                         overlap: int = 50) -> list[tuple[str, str]]:
    """Chunk each Markdown section independently and retain its full heading path."""
    if not text.strip():
        return []

    sections: list[tuple[str, str]] = []
    heading_stack: list[tuple[int, str]] = []
    body_lines: list[str] = []

    def flush() -> None:
        body = "\n".join(body_lines).strip()
        if not body:
            return
        path = " > ".join(label for _, label in heading_stack)
        sections.extend((part, path) for part in chunk_text(body, size=size, overlap=overlap, tokenizer=tokenizer))

    for line in text.splitlines():
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line.strip())
        if heading:
            flush()
            level, label = len(heading.group(1)), heading.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, label))
            body_lines.clear()
        else:
            body_lines.append(line)
    flush()
    return sections


class OfficialIngestionService:
    """Persists only approved official sources; fetch/robots checks stay in the V1 safe Tool layer."""
    def __init__(self, session: AsyncSession, embedder=None) -> None:
        self.session = session
        self.embedder = embedder or shared_embedder()

    async def ingest(self, source: OfficialSource, text: str, metadata: dict[str, Any], embeddings: list[list[float] | None] | None = None) -> KnowledgeDocument:
        if source.status != "verified":
            raise ValueError("Only verified official sources may be ingested")
        body_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        digest = hashlib.sha256((source.url + "|" + str(metadata.get("intake", "")) + "|" + body_hash).encode()).hexdigest()
        existing = await self.session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.content_hash == digest))
        if existing:
            existing.metadata_json = {**existing.metadata_json, **metadata,
                "retrieved_at": metadata.get("retrieved_at", datetime.now(timezone.utc).date().isoformat()), "superseded": False}
            chunks = list((await self.session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == existing.id))).all())
            revision = getattr(self.embedder, "revision", None)
            if chunks and any(c.embedding is None or c.metadata_json.get("embedding_model") != self.embedder.model_name
                    or revision and c.metadata_json.get("embedding_revision") != revision for c in chunks):
                try:
                    vectors = await asyncio.to_thread(self.embedder.passages, [c.content for c in chunks])
                except Exception:
                    vectors = [None] * len(chunks)
                for chunk, vector in zip(chunks, vectors, strict=True):
                    chunk.embedding = vector
            for chunk in chunks:
                chunk.metadata_json = {**chunk.metadata_json, **existing.metadata_json,
                    "embedding_model": self.embedder.model_name, "embedding_revision": getattr(self.embedder, "revision", None)}
            return existing
        previous = list((await self.session.scalars(select(KnowledgeDocument).where(
            KnowledgeDocument.source_id == source.id,
            KnowledgeDocument.metadata_json["intake"].as_string() == str(metadata.get("intake", ""))))).all())
        for old in previous:
            old.metadata_json = {**old.metadata_json, "superseded": True}
        metadata = {**metadata, "retrieved_at": metadata.get("retrieved_at", datetime.now(timezone.utc).date().isoformat()),
                    "chunk_version": "350-50-v1", "body_hash": body_hash, "superseded": False}
        document = KnowledgeDocument(source_id=source.id, source_type="official", authority="official", title=source.title,
                                     url=source.url, content_hash=digest, metadata_json=metadata)
        self.session.add(document)
        await self.session.flush()
        tokenizer = None
        if embeddings is None:
            try:
                tokenizer = await asyncio.to_thread(self.embedder.tokenizer)
            except Exception:
                pass
        pieces, paths, heading = [], [], ""
        for section in re.split(r"(?m)(?=^#{1,6}\s)", text):
            if section.startswith("#"):
                heading = section.splitlines()[0].lstrip("# ")
            for part in chunk_text(section, tokenizer=tokenizer):
                pieces.append(part)
                paths.append(heading)
        if embeddings is None:
            # Lexical retrieval is a valid fallback.  An unavailable local
            # embedding model must not discard a freshly verified official
            # page or make the user wait for a model download.
            try:
                embeddings = await asyncio.to_thread(self.embedder.passages, pieces)
            except Exception:
                embeddings = [None] * len(pieces)
        if len(embeddings) != len(pieces):
            raise ValueError("One embedding entry is required per chunk")
        for index, part in enumerate(pieces):
            embedding = embeddings[index] if embeddings and index < len(embeddings) else None
            self.session.add(KnowledgeChunk(document_id=document.id, chunk_index=index, content=part,
                metadata_json={**metadata, "section_path": paths[index], "lexical_text": lexical_text(part + " " + source.title),
                               "tokenizer_mode": "model" if tokenizer else "unicode_fallback",
                               "embedding_model": self.embedder.model_name,
                               "embedding_revision": getattr(self.embedder, "revision", None)}, embedding=embedding))
        await self.session.flush()
        return document
