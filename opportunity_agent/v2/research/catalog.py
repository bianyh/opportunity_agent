"""Parameterized public catalogue queries and field-level provenance."""
from __future__ import annotations

import json
from datetime import date
from sqlalchemy import select, func, or_, and_
import re

from ..agents.contracts import Evidence, ProgramResult, ResearchFact, merge_evidence
from ..db.models import OfficialSource, ResearchProgram, ResearchRequirement
from .quality import program_matches
from .identity import school_aliases, program_aliases, normalize_intake, intake_aliases
from .temporal import CURRENT_POLICY_PREFIX


class ResearchCatalog:
    def __init__(self, session):
        self.session = session

    async def identities(self):
        return list((await self.session.scalars(select(ResearchProgram).order_by(ResearchProgram.id).limit(2000))).all())

    async def search(self, task, *, include_unknown=False):
        statement = select(ResearchProgram)
        if task.entities.program_family == "computer_science":
            statement = statement.where(or_(
                func.lower(ResearchProgram.program).like("%computer science%"),
                func.lower(ResearchProgram.program).in_(["mscs", "mcs", "cse", "ms cse"])))
        if task.entities.targets:
            statement = statement.where(or_(*(and_(
                func.lower(ResearchProgram.university).in_([a.casefold() for a in school_aliases(t.university)]),
                func.lower(ResearchProgram.program).in_([a.casefold() for a in program_aliases(t.program)])) for t in task.entities.targets)))
        for name in ("university", "program", "intake", "country"):
            value = getattr(task.entities, name)
            scope = getattr(task.entities, {"university": "universities", "program": "programs", "country": "countries"}.get(name, "intake"))
            if name != "intake" and scope:
                values = [alias for v in scope for alias in (school_aliases(v) if name == "university"
                    else program_aliases(v) if name == "program" else (v,))]
                statement = statement.where(func.lower(getattr(ResearchProgram, name)).in_([v.casefold() for v in values]))
            elif value:
                if name == "intake":
                    value = normalize_intake(value)
                if name == "intake" and re.fullmatch(r"20\d{2}", value):
                    statement = statement.where(or_(ResearchProgram.intake == value,
                        ResearchProgram.intake.like(value + " %"), ResearchProgram.intake.like("% " + value)))
                    continue
                values = school_aliases(value) if name == "university" else program_aliases(value) if name == "program" else intake_aliases(value) if name == "intake" else (value,)
                statement = statement.where(func.lower(getattr(ResearchProgram, name)).in_([v.casefold() for v in values]))
        # EXISTS narrows SQL candidates. All observations are still checked for conflicts below.
        if not include_unknown:
            for field in ("deadline", "gre_policy"):
                rr = ResearchRequirement
                c = task.structured_filters
                predicate = [rr.program_id == ResearchProgram.id, rr.field == field, rr.status == "verified"]
                if field == "deadline" and (c.deadline_after or c.deadline_before):
                    if c.deadline_after:
                        predicate.append(rr.date_value >= c.deadline_after)
                    if c.deadline_before:
                        predicate.append(rr.date_value <= c.deadline_before)
                elif field == "gre_policy" and c.gre_policy != "any":
                    allowed = ["required"] if c.gre_policy == "required" else ["optional", "not_required", "not_accepted"]
                    predicate.append(rr.qualifier.in_(allowed + [CURRENT_POLICY_PREFIX + value for value in allowed]))
                else:
                    continue
                statement = statement.where(select(rr.id).where(*predicate).exists())
        rows = list((await self.session.scalars(statement.order_by(ResearchProgram.id).limit(200))).all())
        results = []
        for row in rows:
            result = await self.result(row, task.as_of)
            if result.identity in task.excluded_programs:
                continue
            if include_unknown or program_matches(result, task.structured_filters, task.as_of):
                results.append(result)
        return results

    async def result(self, row, as_of=None):
        today = as_of or date.today()
        result = ProgramResult(program_id=row.id, university=row.university, program=row.program, intake=row.intake, country=row.country)
        from .program_directory import registered_program
        entry = registered_program(row.university, row.program)
        if entry:
            result.program = entry["program"]
        pairs = (await self.session.execute(select(ResearchRequirement, OfficialSource)
            .join(OfficialSource, ResearchRequirement.source_id == OfficialSource.id)
            .where(ResearchRequirement.program_id == row.id).order_by(ResearchRequirement.id))).all()
        grouped = {}
        for observation, source in pairs:
            verified = observation.status == "verified" and source.status == "verified" and observation.program_match == "exact"
            expired = observation.expires_at and observation.expires_at.date() < today
            status = "stale" if expired else "verified" if verified else "unknown"
            current_policy = observation.qualifier.startswith(CURRENT_POLICY_PREFIX)
            proof = observation.extraction_provenance or {}
            evidence = Evidence(source_id=source.id, url=source.url, title=source.title,
                excerpt=observation.excerpt, content_hash=observation.content_hash, intake="" if current_policy else row.intake,
                temporal_scope="current_policy" if current_policy else "explicit_intake" if proof else "legacy",
                extraction_provenance=proof,
                authority="official" if source.status == "verified" else "rejected",
                program_match=observation.program_match if observation.program_match in {"exact", "unknown", "rejected"} else "unknown",
                retrieved_at=observation.verified_at.date(),
                expires_at=observation.expires_at.date() if observation.expires_at else None,
                supports_fields=[observation.field], relevance_method="sql_exact", relevance_passed=bool(verified and not expired))
            result.evidence.append(evidence)
            fact = ResearchFact(field=observation.field, value=observation.value,
                qualifier=observation.qualifier.removeprefix(CURRENT_POLICY_PREFIX),
                verification_status=status, evidence_ids=[evidence.evidence_id])
            grouped.setdefault(observation.field, []).append(fact)
        for field, facts in grouped.items():
            valid = [f for f in facts if f.verification_status == "verified"]
            from .extraction import program_key
            identities = {program_key(e.extraction_provenance['fact']['program']) for e in result.evidence
                          if e.extraction_provenance.get('fact')}
            units = {e.extraction_provenance.get("fact", {}).get("home_unit", "").casefold()
                     for e in result.evidence if e.extraction_provenance.get("fact", {}).get("home_unit")}
            if len({json.dumps(f.value, sort_keys=True) for f in valid}) > 1 or len(units) > 1 or len(identities) > 1:
                for f in valid:
                    f.verification_status = "conflicting"
            elif valid:
                if field == "deadline":
                    try:
                        result.deadline = date.fromisoformat(str(valid[0].value))
                    except ValueError:
                        valid[0].verification_status = "unknown"
                if field == "gre_policy" and isinstance(valid[0].value, str) and valid[0].value in {"required", "optional", "not_required", "not_accepted"}:
                    result.gre_policy = valid[0].value
            result.facts.extend(facts)
        result.evidence = merge_evidence(result.evidence)
        return result
