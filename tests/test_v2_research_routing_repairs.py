from types import SimpleNamespace
from pathlib import Path
import asyncio

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from opportunity_agent.v2.agents.orchestrator import CustomOrchestrator, HeuristicRouter, HeuristicGoalParser, DeterministicSynthesizer
from opportunity_agent.v2.agents.contracts import ExecutionState, RouteDecision, ResearchResult
from opportunity_agent.v2.research.task import parse_task, SemanticParse, intake_matches
from opportunity_agent.v2.research.service import ResearchService
from opportunity_agent.v2.research.import_corpus import import_corpus
from opportunity_agent.v2.research.catalog import ResearchCatalog
from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db.models import ResearchRequirement, KnowledgeChunk


def request(message):
    return SimpleNamespace(message=message, request_id="routing-test", run_id="routing-test",
        success_criteria=None, missing_task=None, remaining_budget_seconds=15, conversation_context={})


@pytest.mark.parametrize("message", ["如果我不考虑GRE，会有什么影响？", "假如我不再申请美国，会怎样？", "If I stop considering GRE, what happens?"])
def test_hypothetical_does_not_force_profile(message):
    assert CustomOrchestrator._guard(message) == []
    state = ExecutionState(user_id="u", conversation_id="c", run_id="r", request_id="q", message=message)
    route = asyncio.run(HeuristicRouter().route(state))
    assert "profile" not in route.agents


def test_actual_correction_still_forces_profile():
    assert CustomOrchestrator._guard("我不考虑需要GRE的项目") == ["profile"]
    assert CustomOrchestrator._guard("我的托福是110分，如果我不考虑GRE会怎样？") == ["profile"]


def test_model_profile_misroute_is_rejected_before_agent_execution():
    class BadRouter:
        async def route(self, state, forced_agents=()):
            assert not forced_agents
            return RouteDecision(mode="delegate", agents=["profile", "research"], parallel=True, reason="controlled model misroute")

    class Agents:
        called = []

        async def execute(self, name, state, missing_task=None):
            self.called.append(name)
            return ResearchResult(route="sql", status="no_results")

    agents = Agents()
    state = ExecutionState(user_id="u", conversation_id="c", run_id="r", request_id="q",
                           message="如果我不考虑GRE，会有什么影响？")
    result = asyncio.run(CustomOrchestrator(goal_parser=HeuristicGoalParser(), router=BadRouter(),
        agent_client=agents, synthesizer=DeterministicSynthesizer(), max_rounds=1).run(state))
    assert agents.called == ["research"]
    assert result.turn_preferences == []
    assert result.success_criteria.gre_policy == "any"


class ExpandingModel:
    enabled = True

    def generate_structured(self, *args, **kwargs):
        return SemanticParse(requested_fields=["gre_policy", "research"], semantic_questions=["不考GRE的影响"])


def test_model_cannot_turn_policy_implications_into_rag():
    task = parse_task(request("如果我不考虑GRE，会有什么影响？"), llm=ExpandingModel())
    assert task.route == "sql"
    assert task.requested_fields == ["gre_policy"]
    assert task.semantic_questions == []
    assert task.routing_diagnostics["parse_mode"] == "rules"
    assert not task.routing_diagnostics.get("model_parse_used")
    criteria = asyncio.run(HeuristicGoalParser().parse("如果我不考虑GRE，会有什么影响？"))
    assert criteria.gre_policy == "any"


def test_real_semantic_intent_still_uses_hybrid():
    task = parse_task(request("不要求GRE并且有人工智能课程的项目"), llm=ExpandingModel())
    assert task.route == "hybrid"


@pytest.mark.parametrize("query,intake,defaulted", [
    ("CMU MSAII 最新截止日期", "2027", True),
    ("CMU MSAII 春季截止日期", "2027 Spring", True),
    ("CMU MSAII 2028 年截止日期", "2028", False),
    ("CMU MSAII 2028 Fall deadline", "2028 Fall", False),
    ("deadline after 2026-12-01", "2027", True),
])
def test_default_intake_policy(query, intake, defaulted):
    task = parse_task(request(query))
    assert task.entities.intake == intake
    assert task.intake_defaulted is defaulted


def test_year_only_intake_does_not_accept_other_years_or_wrong_seasons():
    assert intake_matches("2027", "2027 Fall")
    assert intake_matches("2027", "2027 Spring")
    assert not intake_matches("2027", "2028 Fall")
    assert not intake_matches("2027 Fall", "2027 Spring")


def test_default_notice_is_visible_and_does_not_claim_user_year():
    state = ExecutionState(user_id="u", conversation_id="c", run_id="r", request_id="q", message="GRE")
    state.research_result = ResearchResult(diagnostics={"task": {"intake_defaulted": True}})
    state.answer = "尚未核实。"
    CustomOrchestrator._add_intake_notice(state)
    assert "默认 2027 年" in state.answer
    assert "未指定学期" in state.answer


def test_year_only_web_target_accepts_source_supported_season(monkeypatch):
    monkeypatch.setenv("RESEARCH_PERSIST_FACTS", "0")
    from opportunity_agent.v2.research.service import WebExtraction, WebFact
    from opportunity_agent.v2.agents.contracts import ProgramResult, SuccessCriteria
    from opportunity_agent.v2.research.quality import field_supported
    monkeypatch.setenv("RESEARCH_QUEUE_INGEST", "0")
    page = {"url": "https://www.cs.cmu.edu/admissions", "title": "MSCS admissions",
            "text": "MSCS admissions for 2027 Fall. GRE is optional."}

    class Model:
        enabled = True

        def generate_structured(self, *args, **kwargs):
            from research_response_fixtures import structured
            return structured(kwargs['context'], [WebFact(
                field="gre_policy", value="optional", quote="GRE is optional.")])

    async def run():
        task = parse_task(request("CMU MSCS GRE"))
        result = ResearchResult()
        service = ResearchService(None, llm=Model())
        await service._accept_page(task, result,
            ProgramResult(university="CMU", program="MSCS", intake="2027"), page)
        assert len(result.programs) == 1
        assert result.programs[0].intake == "2027 Fall"
        assert field_supported(result.programs[0], "gre_policy", SuccessCriteria())

    asyncio.run(run())


def test_empty_catalogue_attempts_web_and_records_reason(monkeypatch):
    monkeypatch.setenv("RESEARCH_WEB_ENABLED", "1")

    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with async_sessionmaker(engine)() as session:
                service = ResearchService(session, llm=SimpleNamespace(enabled=False))
                calls = []

                async def web(task, result, candidates):
                    calls.append(task.route)

                service._web = web
                result = await service.execute(request("GRE政策"))
                assert calls == ["sql"]
                assert result.diagnostics["web_attempted"] is True
                assert result.diagnostics["web_fallback_reason"] == "catalogue_empty"
                assert result.route_history[-1] == {"route": "mcp_web", "reason": "catalogue_empty"}
                assert result.diagnostics["eligible_candidates"] == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_real150_import_idempotent_and_never_promotes_pending_facts():
    directory = Path(__file__).resolve().parents[1] / "deliverables/research/real150"
    if not (directory / "pool-clean.db").exists():
        pytest.skip("Local collected corpus is not part of the source distribution")

    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with async_sessionmaker(engine).begin() as session:
                first = await import_corpus(session, directory)
                assert first["chunks_added"] > 0
                assert first["unreviewed_facts_added"] > 0
                assert first["verified_facts_added"] == 0
                candidates = await ResearchCatalog(session).search(parse_task(request("GRE政策")), include_unknown=True)
                assert len(candidates) == first["programs_added"]
                assert all(p.gre_policy == "unknown" for p in candidates)
                assert all(e.program_match == "unknown" for p in candidates for e in p.evidence)
                second = await import_corpus(session, directory)
                assert all(second[k] == 0 for k in first if k.endswith("_added"))
                assert await session.scalar(select(func.count()).select_from(KnowledgeChunk)) == first["chunks_added"]
                assert await session.scalar(select(func.count()).select_from(ResearchRequirement).where(ResearchRequirement.status == "verified")) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
