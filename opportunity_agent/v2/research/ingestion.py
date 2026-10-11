"""Knowledge writes are centralized here, separate from user state and retrieval."""
from datetime import datetime, timedelta, timezone

from ...official_research import classify_program_page
from ..agents.contracts import ProgramResult
from ..rag.ingest import OfficialIngestionService
from .fact_store import persist_verified_page
from .identity import normalize_intake
from .temporal import source_intake


async def ingest_page(session, payload, *, embedder=None):
    program = ProgramResult(university=payload["university"], program=payload["program"],
                            intake=normalize_intake(payload["intake"]), country=payload.get("country", ""))
    stored = await persist_verified_page(session, program, payload, payload.get("facts", []))
    source = stored["source"]
    _, scope, _ = classify_program_page(program.program, payload["title"], payload["url"], payload["text"])
    semantic = [f["provenance"] for f in payload.get("facts", []) if f.get("provenance")]
    observed_intake = (next((p["intake"] for p in semantic if p["intake"]), "") if semantic
                       else source_intake(program.intake, payload["text"]))
    document = await OfficialIngestionService(session, embedder).ingest(source, payload["text"],
        {"school": source.university, "program": source.program, "intake": program.intake,
         "source_intake": observed_intake, "temporal_scope": "explicit_intake" if observed_intake else "current_policy",
         "program_id": stored["program_id"], "program_match": "exact", "scope": scope,
         "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()})
    return {"program_id": stored["program_id"], "document_id": document.id}
