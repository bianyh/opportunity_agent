"""Research intent parsing. Constraints supplied by the orchestrator are immutable."""
from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..agents.contracts import SuccessCriteria
from .identity import school_aliases, canonical_school, program_aliases, normalize_intake
from .program_directory import registered_program, visual_program_mentions
from ...official_research import OfficialDomainRegistry


class ResearchTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    university: str
    program: str


class ResearchEntities(BaseModel):
    model_config = ConfigDict(extra="forbid")
    university: str = ""
    program: str = ""
    degree: Literal['', 'masters', 'doctoral', 'bachelors'] = ''
    home_unit: str = ''
    intake: str = ""
    country: str = ""
    universities: list[str] = Field(default_factory=list)
    programs: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    targets: list[ResearchTarget] = Field(default_factory=list)
    program_family: Literal["", "computer_science"] = ""


class SemanticParse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entities: ResearchEntities = Field(default_factory=ResearchEntities)
    requested_fields: list[Literal["deadline", "gre_policy", "tuition", "language", "curriculum", "research"]] = Field(default_factory=list)
    semantic_questions: list[str] = Field(default_factory=list)
    clarifications: list[str] = Field(default_factory=list)


class ResearchTaskSpec(BaseModel):
    query: str
    entities: ResearchEntities = Field(default_factory=ResearchEntities)
    structured_filters: SuccessCriteria = Field(default_factory=SuccessCriteria)
    requested_fields: list[str] = Field(default_factory=list)
    semantic_questions: list[str] = Field(default_factory=list)
    freshness_required: bool = False
    target_count: int | None = None
    missing_fields: list[str] = Field(default_factory=list)
    excluded_programs: list[tuple[str, str, str]] = Field(default_factory=list)
    clarifications: list[str] = Field(default_factory=list)
    as_of: date = Field(default_factory=date.today)
    routing_diagnostics: dict = Field(default_factory=dict)
    intake_defaulted: bool = False
    research_progress: dict = Field(default_factory=dict)

    @property
    def route(self) -> str:
        if self.freshness_required:
            return "mcp_web"
        structured = bool(set(self.requested_fields) - {"curriculum", "research"})
        structured |= bool(self.structured_filters.deadline_after or self.structured_filters.deadline_before
                           or self.structured_filters.gre_policy != "any")
        return "hybrid" if structured and self.semantic_questions else "rag" if self.semantic_questions else "sql"


def is_policy_impact_question(query):
    return bool(re.search(r"\bGRE\b|GRE", query, re.I)
                and re.search(r"影响|会怎样|会怎么样|what happens|impact|consequence", query, re.I)
                and not re.search(r"帮我找|推荐|筛选|列出|find|recommend|filter", query, re.I))


def is_gre_policy_question(query):
    """Asking what a policy is is not a preference to filter programmes by it."""
    if re.search(r"找|推荐|筛选|列出|\b(?:find|recommend|filter|list)\b", query, re.I):
        return False
    return bool(re.search(
        r"GRE\s*(?:要求|政策|是否|需要吗|必须吗)|(?:是否|要不要|需不需要|需要不需要).{0,10}GRE"
        r"|\b(?:is|are|does|do)\b.{0,30}\bGRE\b|\bGRE\s+(?:requirements?|policy)\b", query, re.I))


def parse_task(request, catalogue=(), llm=None) -> ResearchTaskSpec:
    query = request.message
    lower = query.casefold()
    criteria = (request.success_criteria or SuccessCriteria()).model_copy(deep=True)
    task = ResearchTaskSpec(query=query, structured_filters=criteria,
        target_count=criteria.required_program_count,
        research_progress=getattr(request, "research_progress", {}),
        freshness_required=bool(re.search(r"最新|今年.*(?:改|官网)|重新核验|latest|up.to.date|recheck", lower)))
    for country, pattern in {"US": r"美国|united states|(?<![a-z])usa(?![a-z])", "CA": r"加拿大|canada",
                             "UK": r"英国|united kingdom", "AU": r"澳大利亚|australia"}.items():
        if re.search(pattern, lower):
            task.entities.countries.append(country)
    if len(task.entities.countries) == 1:
        task.entities.country = task.entities.countries[0]
    for field, pattern in {"deadline": r"截止|deadline", "gre_policy": r"(?<![a-z])gre(?![a-z])",
                           "tuition": r"学费|tuition", "language": r"托福|雅思|toefl|ielts"}.items():
        if re.search(pattern, lower):
            task.requested_fields.append(field)
    ai_related = bool(re.search(r"机器学习|人工智能|(?<![a-z])ai(?![a-z])|machine learning", lower))
    for topic, pattern in {"curriculum": r"课程|curricul|course", "research": r"研究方向|实验室|机器学习|人工智能|(?<![a-z])ai(?![a-z])|machine learning|research area|laborator"}.items():
        if re.search(pattern, lower):
            task.requested_fields.append(topic)
            task.semantic_questions.append("机器学习、人工智能相关课程和研究方向 / machine learning AI curriculum research"
                if ai_related else "课程设置 / curriculum courses" if topic == "curriculum"
                else "研究方向和实验室 / research areas laboratories")
    task.semantic_questions = list(dict.fromkeys(task.semantic_questions))
    if criteria.gre_policy != "any" and "gre_policy" not in task.requested_fields:
        task.requested_fields.append("gre_policy")
    if (criteria.deadline_after or criteria.deadline_before) and "deadline" not in task.requested_fields:
        task.requested_fields.append("deadline")
    # Multiple explicitly named targets are a scope, not an ambiguity.
    schools, programs = set(), set()
    def mentioned(alias):
        lowered = str(alias).casefold()
        if lowered.isascii() and len(lowered) <= 4:
            return bool(re.search(r"(?<![a-z0-9])" + re.escape(lowered) + r"(?![a-z0-9])", lower))
        return lowered in lower
    for item in OfficialDomainRegistry().items:
        if any(mentioned(alias) for alias in [item["name"], *item.get("aliases", [])]):
            schools.add(canonical_school(item["name"]))
    for abbreviation in ("MSCS", "MSML", "MSAII", "MCS", "CSE"):
        if any(mentioned(alias) for alias in program_aliases(abbreviation)):
            programs.add(abbreviation)
    for row in catalogue:
        if schools and canonical_school(row.university) not in schools:
            continue
        if any(mentioned(alias) for alias in school_aliases(row.university)):
            schools.add(canonical_school(row.university))
        if mentioned(row.program):
            if not any(p.casefold() == row.program.casefold() for p in programs):
                programs.add(row.program)
        for alias in row.aliases or []:
            if mentioned(alias):
                programs = {p for p in programs if p.casefold() != alias.casefold()}
                programs.add(row.program)
    corrections = []
    if len(schools) == 1:
        school = next(iter(schools))
        corrections = visual_program_mentions(query, school)
        programs.update(item["resolved"] for item in corrections)
        # A school-local official full name gives the source verifier a real
        # identity, not a global acronym expansion asserted to be page evidence.
        programs = {entry["program"] if (entry := registered_program(school, p)) else p for p in programs}
    if corrections:
        task.routing_diagnostics["entity_corrections"] = corrections
    if len(schools) == 1:
        task.entities.university = next(iter(schools))
    if len(programs) == 1:
        task.entities.program = next(iter(programs))
    task.entities.universities = sorted(schools)
    task.entities.programs = sorted(programs)
    if not programs and re.search(r"计算机(?:科学)?(?:专业)?硕士|计算机科学|computer science", lower):
        task.entities.program_family = "computer_science"
    # Preserve explicit local pairings, e.g. CMU MSCS、UIUC MCS. Only
    # use pair restrictions when every named school is paired in its clause;
    # school lists followed by a shared programme list remain independent scope.
    paired = []
    for clause in re.split(r"[，,、；;。\n]|\band\b|以及|和", query, flags=re.I):
        clause_lower = clause.casefold()
        local_schools = [s for s in schools if any(
            re.search(r"(?<![a-z0-9])" + re.escape(a.casefold()) + r"(?![a-z0-9])", clause_lower)
            if a.isascii() else a.casefold() in clause_lower for a in school_aliases(s))]
        local_programs = [p for p in programs if any(re.search(
            r"(?<![a-z0-9])" + re.escape(a.casefold()) + r"(?![a-z0-9])", clause_lower)
            for a in (*program_aliases(p), *(c["input"] for c in corrections if c["resolved"] in program_aliases(p))))]
        if len(local_schools) == 1 and len(local_programs) == 1:
            paired.append(ResearchTarget(university=local_schools[0], program=local_programs[0]))
    if schools and {p.university for p in paired} == schools:
        task.entities.targets = list({(p.university, p.program): p for p in paired}.values())
    year = re.search(r"(20\d{2})\s*(fall|spring|autumn|summer)|(?:fall|spring|autumn|summer)\s*(20\d{2})|(?:秋季|春季|夏季)(20\d{2})|(?:入学|intake)\s*(20\d{2})|(20\d{2})年?(?:秋季|春季|夏季|入学)", lower)
    if year:
        task.entities.intake = normalize_intake(year.group(0))
        # Prefer the exact catalogue spelling when the season/year identify it.
        target_year = next(g for g in year.groups() if g and re.fullmatch(r"20\d{2}", g))
        matches = {r.intake for r in catalogue if target_year in r.intake and intake_supported(r.intake, query)
                   and (not task.entities.program or r.program == task.entities.program)
                   and (not task.entities.university or canonical_school(r.university) == canonical_school(task.entities.university))}
        if len(matches) == 1:
            task.entities.intake = matches.pop()
    if not year:
        # Deadline bounds are not the requested admission year. Apply the
        # explicitly configured product default, not the machine's current year.
        without_dates = re.sub(r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|20\d{2}年\d{1,2}月\d{1,2}日?", "", lower)
        bare_year = re.search(r"(?<!\d)(20\d{2})(?!\d)", without_dates)
        target_year = bare_year[1] if bare_year else "2027"
        season = next((label for label, pattern in (("Fall", r"\bfall\b|\bautumn\b|秋季"),
            ("Spring", r"\bspring\b|春季"), ("Summer", r"\bsummer\b|夏季")) if re.search(pattern, lower)), "")
        task.entities.intake = target_year + (" " + season if season else "")
        task.intake_defaulted = bare_year is None
    task.routing_diagnostics["intake_source"] = "default_2027" if task.intake_defaulted else "user_explicit"
    task.routing_diagnostics["parse_mode"] = "rules"
    # Known fields already specify a safe executable task, including a broad
    # discovery query. Do not call a model just because no SINGLE school exists.
    if llm is not None and llm.enabled and not task.requested_fields:
        try:
            parsed = llm.generate_structured(SemanticParse,
            system="Extract only explicitly named research entities and questions. Do not infer an intake year, change constraints, write SQL, or answer. In a programme-discovery request, unnamed universities/programmes are search scope, not ambiguities requiring clarification. Ask only when essential identity is ambiguous. Respect already recognized registry identities.",
            context={"query": query, "criteria": criteria.model_dump(mode="json"),
                     "recognized_entities": task.entities.model_dump(),
                     "intake_policy": "Unspecified admission year defaults to 2027; do not ask for a missing year or invent a semester. Deadline comparison dates do not specify intake.",
                     "conversation": getattr(request, "conversation_context", {})},
                temperature=0, max_tokens=700, thinking=False)
        except Exception as exc:
            task.routing_diagnostics.update(parse_mode="rule_fallback", model_parse_error=type(exc).__name__)
            parsed = SemanticParse()
        else:
            task.routing_diagnostics["parse_mode"] = "model"
        for key, value in parsed.entities.model_dump().items():
            if value and not getattr(task.entities, key) and not (
                key == "university" and task.entities.universities or key == "program" and task.entities.programs
                or key == "country" and task.entities.countries):
                setattr(task.entities, key, value)
        names = {"美国": "US", "united states": "US", "usa": "US", "us": "US",
            "加拿大": "CA", "canada": "CA", "英国": "UK", "united kingdom": "UK",
            "澳大利亚": "AU", "australia": "AU"}
        task.entities.country = names.get(task.entities.country.casefold(), task.entities.country)
        # Explaining a structured policy's implications is synthesis, not a
        # request to search for unrelated curriculum/research evidence.
        structured_only = bool(task.requested_fields) and not task.semantic_questions and not re.search(
            r"课程|培养|研究|实验室|教授|就业|工作|方向|适合|匹配|人工智能|机器学习|\bAI\b|curricul|course|research|laborator|faculty|career|employment|\bfit\b", query, re.I)
        fields = [f for f in parsed.requested_fields if not structured_only or f not in {"curriculum", "research"}]
        task.requested_fields = list(dict.fromkeys(task.requested_fields + fields))
        if structured_only:
            task.routing_diagnostics["semantic_expansion_rejected"] = bool(parsed.semantic_questions or set(parsed.requested_fields) & {"curriculum", "research"})
        else:
            task.semantic_questions = list(dict.fromkeys(task.semantic_questions + parsed.semantic_questions))
        task.routing_diagnostics["model_parse_used"] = True
        task.clarifications += [c for c in parsed.clarifications if not (task.intake_defaulted
            and re.search(r"(?:入学|申请|目标).{0,8}(?:年份|哪一年)|(?:年份|哪一年).{0,8}(?:入学|申请)|intake year|admission year", c, re.I))]
    task.clarifications += criteria.needs_user_input
    if request.missing_task:
        task.target_count = request.missing_task.required_count or task.target_count
        task.missing_fields = list(request.missing_task.missing_fields)
        task.excluded_programs = [(canonical_school(u), p.casefold(), i.casefold())
            for u, p, i in request.missing_task.excluded_programs]
        task.requested_fields = list(dict.fromkeys(task.requested_fields + task.missing_fields))
    if not task.requested_fields:
        task.requested_fields = ["research"]
        task.semantic_questions = [query]
    task.routing_diagnostics["reason"] = "freshness" if task.freshness_required else "structured_and_semantic" if task.route == "hybrid" else "semantic" if task.route == "rag" else "structured_fields"
    return task


def intake_supported(intake: str, text: str) -> bool:
    years = re.findall(r"20\d{2}", intake)
    if not years or not all(year in text for year in years):
        return False
    for names in (("fall", "autumn", "秋季"), ("spring", "春季"), ("summer", "夏季")):
        if any(name in intake.casefold() for name in names) and not any(name in text.casefold() for name in names):
            return False
    return True


def intake_matches(expected: str, observed: str) -> bool:
    """A year-only request accepts source-supported seasons in that year."""
    from .identity import normalize_intake
    expected, observed = normalize_intake(expected), normalize_intake(observed)
    if re.fullmatch(r"20\d{2}", expected):
        return bool(re.match(re.escape(expected) + r"(?:\s|$)", observed))
    return expected.casefold() == observed.casefold()
