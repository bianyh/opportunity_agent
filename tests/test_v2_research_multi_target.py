"""Controlled scope/provenance tests, not a retrieval-quality benchmark."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from opportunity_agent.v2.agents.contracts import ProgramResult, ResearchResult
from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db.models import OfficialSource, ResearchProgram, ResearchRequirement
from opportunity_agent.v2.research.catalog import ResearchCatalog
from opportunity_agent.v2.research.identity import canonical_school
from opportunity_agent.v2.research.service import ResearchService
from opportunity_agent.v2.research.task import parse_task


def request(query):
    return SimpleNamespace(message=query, request_id="multi", run_id="multi", success_criteria=None,
                           missing_task=None, conversation_context={}, remaining_budget_seconds=15)


class NoModelCalls:
    enabled = True

    def generate_structured(self, *args, **kwargs):
        raise AssertionError("Known structured queries must not call model parsing")


HISTORICAL_QUERY = ("评估如果申请者不提交 GRE，对其申请美国/加拿大计算机硕士项目的影响。"
                    "目标学校包括 CMU、UIUC、UCSD、Georgia Tech、UBC；目标项目类型包括 MSCS、MCS、CSE。"
                    "需要核实这些项目当前 GRE 要求或可选政策，并分析不提交 GRE 对选校范围和申请竞争力的影响。")


def test_historical_query_preserves_all_scopes_without_model_or_clarification():
    task = parse_task(request(HISTORICAL_QUERY), llm=NoModelCalls())
    assert len(task.entities.universities) == 5
    assert set(task.entities.programs) == {"MSCS", "MCS", "CSE"}
    assert set(task.entities.countries) == {"US", "CA"}
    assert task.entities.country == ""
    assert task.entities.university == ""
    assert not task.entities.targets  # Independent lists are not explicit pairings.
    assert not task.clarifications
    assert task.route == "sql"
    assert task.structured_filters.gre_policy == "any"
    assert task.routing_diagnostics["parse_mode"] == "rules"


def test_explicit_pairs_are_not_a_cartesian_product():
    task = parse_task(request("比较 CMU MSCS、UIUC MCS 的 GRE"), llm=NoModelCalls())
    assert {(t.university, t.program) for t in task.entities.targets} == {
        (canonical_school("CMU"), "MSCS"), (canonical_school("UIUC"), "MCS")}


def test_semantic_model_failure_degrades_to_query_instead_of_aborting():
    class SlowModel:
        enabled = True

        def generate_structured(self, *args, **kwargs):
            raise TimeoutError("upstream model unavailable")

    task = parse_task(request("推荐适合我的项目"), llm=SlowModel())
    assert task.route == "rag"
    assert task.semantic_questions == ["推荐适合我的项目"]
    assert task.routing_diagnostics["model_parse_error"] == "TimeoutError"


async def seeded():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        for index, (school, program, country) in enumerate([
            ("CMU", "MSCS", "US"), ("UIUC", "MCS", "US"),
            ("CMU", "MCS", "US"), ("UIUC", "MSCS", "US"),
            ("UBC", "MSCS", "CA"), ("UCSD", "MSCS", "US")]):
            pid, sid = f"p{index}", f"s{index}"
            session.add(ResearchProgram(id=pid, university=school, program=program, country=country, intake="2027 Fall"))
            session.add(OfficialSource(id=sid, source_key=sid, university=school, program=program,
                                       url=f"https://www.cmu.edu/{sid}", title=program))
            await session.flush()
            session.add(ResearchRequirement(program_id=pid, source_id=sid, field="gre_policy", value="optional",
                qualifier="optional", status="verified", program_match="exact", excerpt="GRE is optional.",
                content_hash="hash", verified_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(days=30)))
    return engine, factory


def test_sql_explicit_pairs_exclude_cross_pairs_and_other_schools():
    async def run():
        engine, factory = await seeded()
        try:
            async with factory() as session:
                task = parse_task(request("比较 CMU MSCS、UIUC MCS 的 GRE"))
                results = await ResearchCatalog(session).search(task)
                assert {(p.university, p.program) for p in results} == {("CMU", "MSCS"), ("UIUC", "MCS")}
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_sql_independent_scope_includes_multiple_countries_and_excludes_unrequested_school():
    async def run():
        engine, factory = await seeded()
        try:
            async with factory() as session:
                task = parse_task(request("美国/加拿大 CMU、UIUC、UBC 的 MSCS、MCS GRE 政策"))
                results = await ResearchCatalog(session).search(task)
                assert {p.university for p in results} == {"CMU", "UIUC", "UBC"}
                assert {p.country for p in results} == {"US", "CA"}
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_full_degree_names_match_abbreviations_without_conflating_degrees(monkeypatch):
    from sqlalchemy import update
    monkeypatch.setenv("RESEARCH_WEB_ENABLED", "0")
    async def run():
        engine, factory = await seeded()
        try:
            async with factory.begin() as session:
                await session.execute(update(ResearchProgram).where(ResearchProgram.program == "MSCS").values(
                    program="Master of Science in Computer Science"))
                await session.execute(update(ResearchProgram).where(ResearchProgram.program == "MCS").values(
                    program="Master of Computer Science"))
            async with factory() as session:
                result = await ResearchService(session, llm=NoModelCalls()).execute(
                    request("CMU MSCS、UIUC MCS GRE"))
                assert result.status == "complete", result.model_dump()
                assert {(p.university, p.program) for p in result.programs} == {
                    ("CMU", "Master of Science in Computer Science"), ("UIUC", "Master of Computer Science")}
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_multi_target_service_reaches_sql_and_returns_all_verified_pairs(monkeypatch):
    monkeypatch.setenv("RESEARCH_WEB_ENABLED", "0")
    async def run():
        engine, factory = await seeded()
        try:
            async with factory() as session:
                service = ResearchService(session, llm=NoModelCalls())
                result = await service.execute(request("比较 CMU MSCS、UIUC MCS 的 GRE"))
                assert result.status == "complete", result.model_dump()
                assert len(result.programs) == 2
                assert result.diagnostics["catalogue_candidates"] == 2
                assert not result.errors
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_missing_school_is_partial_not_complete_and_web_gets_missing_target(monkeypatch):
    async def run():
        engine, factory = await seeded()
        try:
            async with factory() as session:
                service = ResearchService(session, llm=NoModelCalls())
                calls = []
                async def web(task, result, candidates):
                    calls.extend(service._web_targets(task, result, candidates))
                service._web = web
                monkeypatch.setenv("RESEARCH_WEB_ENABLED", "1")
                result = await service.execute(request("比较 CMU MSCS、UIUC MCS、UBC CSE 的 GRE"))
                assert result.status == "partial"
                assert any(m.get("kind") == "missing_target" and m.get("university") == canonical_school("UBC")
                           for m in result.missing_items)
                assert [(p.university, p.program) for p in calls] == [(canonical_school("UBC"), "CSE")]
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_rag_empty_catalog_keeps_explicit_pair_filters():
    class Retriever:
        def __init__(self):
            self.calls = []
        async def search(self, question, *, filters, **kwargs):
            self.calls.append(filters)
            return [], {}
    async def run():
        retriever = Retriever()
        service = ResearchService(None, llm=NoModelCalls(), retriever=retriever)
        task = parse_task(request("CMU MSCS、UIUC MCS 的课程"))
        await service._rag(task, ResearchResult(), None)
        assert {(f["school"], f["program"]) for f in retriever.calls} == {
            (canonical_school("CMU"), "MSCS"), (canonical_school("UIUC"), "MCS")}
    asyncio.run(run())


def test_repair_preserves_prior_pair_and_queries_only_unfinished_pair(monkeypatch):
    from opportunity_agent.v2.agents.contracts import MissingTask, ExecutionState
    from opportunity_agent.v2.agents.result_aggregation import ResultAggregator
    monkeypatch.setenv("RESEARCH_WEB_ENABLED", "0")
    async def run():
        engine, factory = await seeded()
        try:
            async with factory() as session:
                service = ResearchService(session, llm=NoModelCalls())
                first = await service.execute(request("CMU MSCS GRE"))
                repair = request("CMU MSCS、UIUC MCS 的 GRE")
                repair.missing_task = MissingTask(agent="research", reason="补查 UIUC",
                    excluded_programs=[p.identity for p in first.programs])
                second = await service.execute(repair)
                assert second.status == "complete", second.model_dump()
                assert [p.university for p in second.programs] == ["UIUC"]
                assert not second.missing_items
                state = ExecutionState(user_id="u", conversation_id="c", run_id="r", request_id="q", message=repair.message)
                aggregator = ResultAggregator()
                aggregator.merge_research(state, first)
                aggregator.merge_research(state, second)
                assert len(state.research_result.programs) == 2
                assert state.research_result.status == "complete"
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_model_parse_timeout_still_reaches_catalogue_and_rag(monkeypatch):
    class TimeoutModel:
        enabled = True
        def generate_structured(self, *args, **kwargs):
            raise TimeoutError("upstream timeout")
    class Retriever:
        async def search(self, *args, **kwargs):
            return [], {}
    monkeypatch.setenv("RESEARCH_WEB_ENABLED", "0")
    async def run():
        engine, factory = await seeded()
        try:
            async with factory() as session:
                result = await ResearchService(session, llm=TimeoutModel(), retriever=Retriever()).execute(
                    request("CMU MSCS 适合我吗"))
                assert result.diagnostics["catalogue_candidates"] == 1
                assert result.diagnostics["task"]["routing"]["model_parse_error"] == "TimeoutError"
                assert result.route == "rag"
                assert not result.errors
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_web_merges_pages_for_same_year_only_target(monkeypatch):
    from opportunity_agent.v2.research.service import WebExtraction, WebFact
    class Model:
        enabled = True
        def generate_structured(self, *args, **kwargs):
            from research_response_fixtures import structured
            return structured(kwargs['context'], [WebFact(
                field="gre_policy", value="optional", quote="GRE is optional.")])
    async def run():
        monkeypatch.setenv("RESEARCH_QUEUE_INGEST", "0")
        service = ResearchService(None, llm=Model())
        task, result = parse_task(request("CMU MSCS GRE")), ResearchResult()
        target = ProgramResult(university="CMU", program="MSCS", intake="2027")
        for suffix in ("one", "two"):
            await service._accept_page(task, result, target, {"url": f"https://www.cs.cmu.edu/{suffix}",
                "title": "MSCS admissions", "text": "MSCS 2027 Fall admissions. GRE is optional."})
        assert len(result.programs) == 1
    asyncio.run(run())


def test_multi_school_web_budget_is_shared_and_searches_are_target_scoped(monkeypatch):
    import opportunity_agent.v2.research.service as module
    instances, accepted = [], []
    class Web:
        def __init__(self, *, search_limit, page_limit):
            self.search_limit, self.page_limit = search_limit, page_limit
            self.search_calls = self.page_calls = 0
            self.queries = []
            instances.append(self)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def search(self, query, domains):
            self.search_calls += 1
            self.queries.append(query)
            return {"results": [{"url": f"https://{domains[0]}/{i}"} for i in range(5)]}
        async def read(self, url, domains):
            self.page_calls += 1
            return {"url": url, "title": "fixture", "text": "fixture"}
    monkeypatch.setattr(module, "TavilyMCP", Web)
    async def run():
        service = ResearchService(None, llm=NoModelCalls(), web_factory=Web)
        async def accept(task, result, target, page):
            accepted.append(target.university)
        service._accept_page = accept
        task = parse_task(request("CMU、UIUC、UCSD、Georgia Tech、UBC 的 MSCS GRE 政策"))
        result = ResearchResult()
        await service._web(task, result, [])
        assert not result.errors
        assert instances[0].search_calls == 5
        assert instances[0].page_calls == 10
        assert len(set(accepted)) == 5
        assert len(result.diagnostics["web_targets_attempted"]) == 5
        assert all("、" not in q for q in instances[0].queries)
    asyncio.run(run())


def test_semantic_parse_wall_budget_falls_back_without_waiting_for_model(monkeypatch):
    import time
    import opportunity_agent.v2.research.service as module
    original_parse, original_timeout = module.parse_task, asyncio.timeout
    def slow_parse(req, catalogue=(), llm=None):
        if llm is not None:
            time.sleep(.1)
        return original_parse(req, catalogue)
    monkeypatch.setattr(module, "parse_task", slow_parse)
    monkeypatch.setattr(module.asyncio, "timeout", lambda delay: original_timeout(.01 if delay == 7 else delay))
    monkeypatch.setenv("RESEARCH_WEB_ENABLED", "0")
    class Retriever:
        async def search(self, *args, **kwargs):
            return [], {}
    async def run():
        engine, factory = await seeded()
        try:
            async with factory() as session:
                result = await ResearchService(session, llm=NoModelCalls(), retriever=Retriever()).execute(
                    request("CMU MSCS 适合我吗"))
                assert result.diagnostics["task"]["routing"]["model_parse_error"] == "parse_budget_exhausted"
                assert result.diagnostics["catalogue_candidates"] == 1
        finally:
            await engine.dispose()
    asyncio.run(run())
