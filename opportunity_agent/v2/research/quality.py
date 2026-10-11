"""Deterministic field/evidence gates shared by research and completion checking."""
from datetime import date

from ..agents.contracts import Evidence, ProgramResult, SuccessCriteria


def usable(e: Evidence, criteria: SuccessCriteria, as_of: date | None = None) -> bool:
    today = as_of or date.today()
    if not (e.source_id and e.url and e.authority in criteria.accepted_authorities):
        return False
    if e.expires_at and e.expires_at < today:
        return False
    if e.program_match == "rejected":
        return False
    if e.relevance_method == "sql_exact":
        return bool(e.excerpt and e.program_match == "exact" and e.relevance_passed is True)
    if e.relevance_method != "legacy":
        return bool(e.excerpt and e.relevance_passed is True)
    return e.relevance_score is not None and e.relevance_score >= criteria.minimum_relevance_score


def intake_bound(program: ProgramResult, evidence: Evidence) -> bool:
    if evidence.temporal_scope == "current_policy":
        return not evidence.intake and evidence.retrieved_at is not None and evidence.expires_at is not None
    return evidence.intake.casefold() == program.intake.casefold()


def field_supported(program: ProgramResult, field: str, criteria: SuccessCriteria, as_of=None) -> bool:
    # Old serialized runs remain readable, but newly built results must bind fields.
    if not program.facts and not program.program_id:
        return any(usable(e, criteria, as_of) for e in program.evidence)
    observations = [f for f in program.facts if f.field == field]
    if any(f.verification_status == "conflicting" for f in observations):
        return False
    evidence = {e.evidence_id: e for e in program.evidence}
    def matches_scalar(fact):
        if field == "deadline":
            return program.deadline is not None and str(fact.value) == program.deadline.isoformat()
        if field == "gre_policy":
            return fact.value == program.gre_policy
        return True
    return any(f.verification_status == "verified" and any(
        eid in evidence and field in evidence[eid].supports_fields
        and evidence[eid].program_match == "exact"
        and intake_bound(program, evidence[eid])
        and usable(evidence[eid], criteria, as_of)
        for eid in f.evidence_ids) and matches_scalar(f) for f in observations)


def program_matches(program: ProgramResult, criteria: SuccessCriteria, as_of=None) -> bool:
    if program.required_country and program.country.casefold() != program.required_country.casefold():
        return False
    if any(not field_supported(program, field, criteria, as_of) for field in program.required_fields):
        return False
    if criteria.gre_policy == "not_required" and program.gre_policy not in {"not_required", "optional", "not_accepted"}:
        return False
    if criteria.gre_policy == "required" and program.gre_policy != "required":
        return False
    if criteria.deadline_after and (program.deadline is None or program.deadline < criteria.deadline_after):
        return False
    if criteria.deadline_before and (program.deadline is None or program.deadline > criteria.deadline_before):
        return False
    if criteria.gre_policy != "any" and not field_supported(program, "gre_policy", criteria, as_of):
        return False
    if (criteria.deadline_after or criteria.deadline_before) and not field_supported(program, "deadline", criteria, as_of):
        return False
    return not (criteria.evidence_required or criteria.citation_required) or any(usable(e, criteria, as_of) for e in program.evidence)
