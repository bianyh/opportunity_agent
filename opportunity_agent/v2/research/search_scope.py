"""Search text is a hint, never authority to change the parsed research scope."""
import re
from types import SimpleNamespace

from ...official_research import OfficialDomainRegistry
from .identity import canonical_school, canonical_program, normalize_intake, program_aliases
from .repair import ToolFailure
from .task import parse_task


def contains(text, alias):
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(alias.casefold()) + r"(?![a-z0-9])", text.casefold())) if alias.isascii() else alias.casefold() in text.casefold()


def program_mentions(text):
    # CSE's full name contains the MSCS full name; only the longest identity counts.
    matches = [(m.start(), m.end(), canonical_program(name))
        for name in ("MSCS", "MCS", "CSE", "MSML", "MSAII") for alias in program_aliases(name)
        for m in re.finditer(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", text, re.I)]
    return {name for start, end, name in matches if not any(
        a <= start and b >= end and b-a > end-start for a, b, _ in matches)}


def scoped_query(task, target, proposed):
    """Reject explicit conflicts; fill omitted anchors from immutable server state."""
    if re.search(r"这些|那些|上述|同上|\b(?:it|them|those|these)\b", proposed, re.I):
        raise ToolFailure("QUERY_SCOPE_REJECTED")
    schools = {canonical_school(item["name"]) for item in OfficialDomainRegistry().items
               if any(contains(proposed, a) for a in [item["name"], *item.get("aliases", [])])}
    allowed = ({canonical_school(target.university)} if target.university else
               {canonical_school(x) for x in [task.entities.university, *task.entities.universities,
                                             *(t.university for t in task.entities.targets)] if x})
    if allowed and schools - allowed:
        raise ToolFailure("QUERY_SCOPE_REJECTED")
    intake = target.intake or task.entities.intake
    years = set(re.findall(r"\b20\d{2}\b", proposed))
    year = set(re.findall(r"20\d{2}", intake))
    if year and years - year:
        raise ToolFailure("QUERY_SCOPE_REJECTED")
    parsed = parse_task(SimpleNamespace(message=proposed, success_criteria=None, missing_task=None))
    countries = set(task.entities.countries or ([task.entities.country] if task.entities.country else []))
    if countries and set(parsed.entities.countries) - countries:
        raise ToolFailure("QUERY_SCOPE_REJECTED")
    if parsed.entities.intake and len(normalize_intake(parsed.entities.intake).split()) > 1:
        actual, expected = normalize_intake(parsed.entities.intake), normalize_intake(intake)
        if len(expected.split()) > 1 and actual != expected:
            raise ToolFailure("QUERY_SCOPE_REJECTED")
    if target.program and any(canonical_program(target.program) != canonical_program(p)
                              for p in program_mentions(proposed)):
        raise ToolFailure("QUERY_SCOPE_REJECTED")
    if task.structured_filters.gre_policy == "not_required" and re.search(
            r"\bGRE\s+(?:is\s+)?(?:required|mandatory)\b|GRE必须|(?<!不)要求GRE", proposed, re.I):
        raise ToolFailure("QUERY_SCOPE_REJECTED")
    anchor = " ".join(filter(None, [target.university, target.program, intake,
        {"US": "United States", "CA": "Canada", "UK": "United Kingdom", "AU": "Australia"}.get(task.entities.country, "")]))
    # Keep the anchors even when a long rewrite needs truncating. Avoid growth on replay.
    proposed = " ".join(proposed.split())
    if anchor and proposed.casefold().startswith(anchor.casefold()):
        return proposed[:500]
    return (anchor + " " + proposed).strip()[:500]


def initial_query(task, target, field=None):
    fields = [field] if field else task.requested_fields
    terms = {"deadline": "application deadline", "gre_policy": "GRE requirements optional not required",
             "language": "English language TOEFL IELTS requirements", "tuition": "tuition fees",
             "curriculum": "curriculum courses", "research": "research areas"}
    hint = (" " + field + " official policy") if field else " " + " ".join(task.semantic_questions)
    return scoped_query(task, target, "official graduate admissions " + " ".join(terms.get(f, f) for f in fields) + hint)
