"""Evergreen official policies are usable without inventing an admission cycle."""
import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from opportunity_agent.v2.agents.contracts import ProgramResult, ResearchResult, SuccessCriteria, ExecutionState
from opportunity_agent.v2.agents.orchestrator import DeterministicSynthesizer, LLMSynthesizer
from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.research.catalog import ResearchCatalog
from opportunity_agent.v2.research.fact_store import persist_verified_page
from opportunity_agent.v2.research.normalization import supported_value
from opportunity_agent.v2.research.quality import field_supported, program_matches
from opportunity_agent.v2.research.service import ResearchService, WebExtraction, WebFact
from opportunity_agent.v2.research.task import parse_task
from opportunity_agent.v2.research.temporal import source_intake
from opportunity_agent.v2.rag.retrieval import RetrievalHit


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setenv("RESEARCH_PERSIST_FACTS", "0")
    monkeypatch.setenv("RESEARCH_QUEUE_INGEST", "0")
    # Model-path temporal gates; keyword extraction is covered in test_v2_deadline_extraction.
    monkeypatch.setenv("RESEARCH_KEYWORD_EXTRACTION", "0")


def task():
    value = parse_task(SimpleNamespace(message="Brown MSCS 2027 Fall GRE 不要求和截止日期",
        success_criteria=SuccessCriteria(gre_policy="not_required"), missing_task=None))
    value.as_of = date(2026, 10, 10)
    return value


class Model:
    enabled = True
    def __init__(self, intake="", gre="not_required", deadline="2027-01-15", quote=None):
        self.intake, self.gre, self.deadline, self.quote = intake, gre, deadline, quote
        self.requests = []
    @property
    def calls(self):
        return len(self.requests)
    def generate_structured(self, *args, **kwargs):
        self.requests.append(kwargs)
        return WebExtraction(intake=self.intake, facts=[
            WebFact(field="gre_policy", value=self.gre, quote=f"GRE is {self.gre.replace('_', ' ')}."),
            WebFact(field="deadline", value=self.deadline,
                quote=self.quote or "Application deadline: January 15, 2027.")])


def page(text=None):
    return {"url": "https://brown.edu/mscs", "title": "MSCS admissions", "text": text or
        "MSCS admissions. GRE is not required. Application deadline: January 15, 2027. Copyright 2026."}


@pytest.mark.parametrize("parsed_intake", ["", "2027 Fall"])
@pytest.mark.parametrize("repair_enabled", ["0", "1"])
def test_no_intake_page_is_accepted_but_never_claims_explicit_cycle(parsed_intake, repair_enabled, monkeypatch):
    monkeypatch.setenv("RESEARCH_TOOL_REPAIR_ENABLED", repair_enabled)
    async def run():
        model = Model(parsed_intake)
        service, result = ResearchService(None, llm=model), ResearchResult()
        service._repair_active = repair_enabled == "1"
        await service._accept_page(task(), result, ProgramResult(university="Brown", program="MSCS", intake="2027 Fall"), page())
        assert model.calls == 1
        program = result.programs[0]
        assert program_matches(program, task().structured_filters, task().as_of)
        assert program.intake == "2027 Fall"  # Task identity, not a claim by the source.
        assert all(e.temporal_scope == "current_policy" and e.intake == "" for e in program.evidence)
        assert field_supported(program, "deadline", task().structured_filters, task().as_of)
        program.evidence[0].expires_at = date(2026, 10, 9)
        assert not field_supported(program, "gre_policy", task().structured_filters, task().as_of)
    asyncio.run(run())


def test_explicit_wrong_cycle_is_still_rejected_before_model():
    async def run():
        model, result = Model(), ResearchResult()
        await ResearchService(None, llm=model)._accept_page(task(), result,
            ProgramResult(university="Brown", program="MSCS", intake="2027 Fall"),
            page("MSCS admissions for Fall 2026. GRE is not required."))
        assert model.calls == 0 and not result.programs
    asyncio.run(run())


def test_expired_deadline_is_not_rolled_forward():
    async def run():
        quote = "Application deadline: January 15, 2026."
        result = ResearchResult()
        await ResearchService(None, llm=Model(deadline="2026-01-15", quote=quote))._accept_page(task(), result,
            ProgramResult(university="Brown", program="MSCS", intake="2027 Fall"),
            page("MSCS. GRE is not required. " + quote))
        assert result.programs[0].deadline is None
        assert result.programs[0].gre_policy == "not_required"
        assert result.diagnostics["web_field_rejections"][0]["reason"] == "expired_deadline"
    asyncio.run(run())


@pytest.mark.parametrize("quote,value", [
    ("Early Deadline: Nov. 18, 2026", "2026-11-18"),
    ("Final Deadline: Dec. 9, 2026", "2026-12-09"),
    ("Deadline: January 15th, 2027", "2027-01-15")])
def test_abbreviated_month_dates(quote, value):
    assert supported_value("deadline", value, quote)
    assert not supported_value("deadline", "2028-01-15", quote)


def test_cmu_msaii_named_query_accepts_facts_but_gre_filter_excludes_it():
    async def run():
        result, model = ResearchResult(), Model("2027 Fall", "required", "2026-12-09", "Final Deadline: Dec. 9, 2026")
        target = ProgramResult(university="CMU", program="MSAII", intake="2027 Fall")
        official = {"url": "https://lti.cmu.edu/academics/masters-programs/msaii.html",
            "title": "Master of Science in Artificial Intelligence and Innovation", "text":
            "Master of Science in Artificial Intelligence and Innovation (MSAII). "
            "The application window for the Fall 2027 admissions cycle will open on September 9, 2026. "
            "Early Deadline: Nov. 18, 2026. Final Deadline: Dec. 9, 2026. GRE is required."}
        service = ResearchService(None, llm=model)
        await service._accept_page(task(), result, target, official)
        assert result.programs[0].deadline == date(2026, 12, 9)
        assert result.programs[0].gre_policy == "required"
        assert all(e.temporal_scope == "explicit_intake" and e.intake == "2027 Fall" for e in result.programs[0].evidence)
        assert program_matches(result.programs[0], SuccessCriteria(gre_policy="any"), task().as_of)
        assert not program_matches(result.programs[0], SuccessCriteria(gre_policy="not_required"), task().as_of)
        wrong = ResearchResult()
        await service._accept_page(task(), wrong, target.model_copy(update={"program": "MSCS"}), official)
        assert not wrong.programs and model.calls == 1
    asyncio.run(run())


def test_current_policy_survives_database_round_trip_and_gre_sql_filter():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            result = ResearchResult()
            await ResearchService(None, llm=Model())._accept_page(task(), result,
                ProgramResult(university="Brown", program="MSCS", intake="2027 Fall"), page())
            p = result.programs[0]
            facts = [{"field": f.field, "value": f.value, "qualifier": f.qualifier,
                "quote": next(e.excerpt for e in p.evidence if e.evidence_id in f.evidence_ids)} for f in p.facts]
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                await persist_verified_page(session, p, page(), facts)
                await session.commit()
                retrieved = await ResearchCatalog(session).search(task())
            assert len(retrieved) == 1
            assert program_matches(retrieved[0], task().structured_filters, task().as_of)
            assert all(e.intake == "" and e.temporal_scope == "current_policy" for e in retrieved[0].evidence)
            state = ExecutionState(user_id="u", conversation_id="c", run_id="r", request_id="q", message="GRE")
            state.research_result = ResearchResult(programs=retrieved)
            answer = await DeterministicSynthesizer().synthesize(state)
            assert "适用入学季未确认" in answer
            assert "temporal_scope=current_policy" in LLMSynthesizer._SYSTEM_PROMPT
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_source_years_are_not_intakes_and_multiple_cycles_can_match():
    assert source_intake("2027 Fall", "Deadline January 15, 2027. Copyright 2026. Fall admission.") == ""
    assert source_intake("2027 Fall", "Historical Fall 2026. Current Fall 2027.") == "2027 Fall"


def test_semantic_retrieval_preserves_source_cycle_not_index_scope():
    async def run():
        t = task()
        t.semantic_questions = ["AI curriculum"]
        hit = RetrievalHit("chunk", "doc", "MSCS", "https://brown.edu/mscs", "AI curriculum.", "source",
            .9, .9, .9, {"program_match": "exact", "intake": "2027 Fall", "source_intake": "",
                "temporal_scope": "current_policy", "retrieved_at": "2026-10-10", "expires_at": "2026-11-09"},
            relevance_method="cross_encoder")
        class Retriever:
            async def search(self, *args, **kwargs):
                return [hit], {"reranker": "stub"}
        result = ResearchResult()
        program = ProgramResult(program_id="p", university="Brown", program="MSCS", intake="2027 Fall")
        service = ResearchService(None, llm=Model(), retriever=Retriever(), rerank=True, threshold=.5)
        await service._rag(t, result, program)
        assert len(result.findings) == 1
        assert result.evidence[0].intake == "" and result.evidence[0].temporal_scope == "current_policy"
        assert field_supported(program, "semantic:AI curriculum", t.structured_filters, t.as_of)
    asyncio.run(run())


@pytest.mark.parametrize("explicit_first", [False, True])
def test_year_only_target_merges_evergreen_and_explicit_sources(explicit_first):
    async def run():
        service, result = ResearchService(None, llm=Model()), ResearchResult()
        target = ProgramResult(university="Brown", program="MSCS", intake="2027")
        sources = [page(), page("MSCS Fall 2027. GRE is not required. Application deadline: January 15, 2027.")]
        if explicit_first:
            sources.reverse()
        for source in sources:
            await service._accept_page(task(), result, target, source)
        assert len(result.programs) == 1
        assert result.programs[0].intake == "2027 Fall"
        assert program_matches(result.programs[0], task().structured_filters, task().as_of)
    asyncio.run(run())
