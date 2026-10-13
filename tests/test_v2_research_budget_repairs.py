"""Offline regression of the 2026-10-09 repeated Brown/extraction failure."""
import asyncio
import copy
import time
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from opportunity_agent.v2.agents.a2a import request_from_state
from opportunity_agent.v2.agents.contracts import ExecutionState, ResearchResult, ProgramResult, RouteDecision
from opportunity_agent.v2.agents.orchestrator import CustomOrchestrator, HeuristicGoalParser, HeuristicRouter, DeterministicSynthesizer
from opportunity_agent.v2.core.research_budget import research_seconds, execution_seconds, estimate_school_count
from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db.models import ResearchProgram
from opportunity_agent.v2.research.catalog import ResearchCatalog
from opportunity_agent.v2.research.service import ResearchService, WebExtraction, WebFact
from opportunity_agent.v2.research.task import parse_task
from opportunity_agent.v2.research.failures import failure_summary
from opportunity_agent.v2.agents.result_aggregation import ResultAggregator


QUERY = "查询2027 Fall美国计算机硕士项目，筛选GRE不要求的项目，列出申请截止日期。"


def request(message=QUERY, **kwargs):
    return SimpleNamespace(message=message, request_id="test", run_id="test", success_criteria=None,
                           missing_task=None, conversation_context={}, **kwargs)


def state():
    return ExecutionState(user_id="u", conversation_id="c", run_id="r", request_id="q", message=QUERY)


def targets():
    return [ProgramResult(university=u, program="MSCS", country="US", intake="2027 Fall")
            for u in ("Brown University", "Carnegie Mellon University")]


class Web:
    search_limit = 10
    page_limit = 20

    def __init__(self):
        self.search_calls = self.page_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def search(self, query, domains):
        self.search_calls += 1
        domain = domains[0] if domains else "brown.edu"
        return {"results": [{"url": f"https://{domain}/mscs"}]}

    async def read(self, url, domains):
        self.page_calls += 1
        return {"url": url, "title": "MSCS", "text": "2027 Fall MSCS. GRE is not required."}


def test_budget_scales_with_schools_and_is_capped(monkeypatch):
    monkeypatch.delenv("RESEARCH_BUDGET_SECONDS", raising=False)
    monkeypatch.setenv("RESEARCH_PER_SCHOOL_SECONDS", "60")
    monkeypatch.setenv("RESEARCH_OVERHEAD_SECONDS", "15")
    monkeypatch.setenv("RESEARCH_MAX_BUDGET_SECONDS", "600")
    assert research_seconds(1) == 75
    assert research_seconds(5) == 315
    assert research_seconds(100) == 600
    assert estimate_school_count("CMU、UIUC、UCSD 的 MSCS GRE") == 3
    assert execution_seconds("CMU、UIUC、UCSD 的 MSCS GRE") == 765
    monkeypatch.setenv("RESEARCH_PER_SCHOOL_SECONDS", "nan")
    assert research_seconds(1) == 75
    monkeypatch.setenv("RESEARCH_MAX_BUDGET_SECONDS", "1200")
    execution = state()
    req = request_from_state("research", execution)
    assert req.remaining_budget_seconds == 615
    assert research_seconds(20) == 1200


def test_service_refines_budget_using_unique_schools_not_programs(monkeypatch):
    monkeypatch.setenv("RESEARCH_WEB_ENABLED", "0")
    async def run():
        service = ResearchService(None, llm=SimpleNamespace(enabled=False))
        rows = targets() + [targets()[0].model_copy(update={"program": "MCS"})]
        class Catalog:
            async def identities(self): return []
            async def search(self, task, **kwargs): return rows if kwargs else []
        service.catalog = Catalog()
        result = await service.execute(request(remaining_budget_seconds=500))
        assert result.diagnostics["budget_school_count"] == 2, result.model_dump()
        assert result.diagnostics["budget_seconds"] == 135
        limited = await service.execute(request(remaining_budget_seconds=10))
        assert limited.diagnostics["budget_seconds"] == 10
    asyncio.run(run())


def test_chinese_cs_scope_filters_out_ece_in_sql():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory.begin() as session:
                for i, program in enumerate(["MSCS", "MCS", "CSE", "Master of Science in Computer Science",
                                             "Master of Science in Electrical and Computer Engineering"]):
                    session.add(ResearchProgram(id=str(i), university="CMU", program=program, country="US", intake="2027 Fall"))
            task = parse_task(request())
            assert task.entities.program_family == "computer_science"
            async with factory() as session:
                rows = await ResearchCatalog(session).search(task, include_unknown=True)
                assert len(rows) == 4
                assert all("Electrical" not in row.program for row in rows)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_first_school_extraction_timeout_does_not_abort_second_school(monkeypatch):
    monkeypatch.setenv("RESEARCH_KEYWORD_EXTRACTION", "0")  # force the model path for Brown
    class Model:
        enabled = True
        def generate_structured(self, *args, **kwargs):
            if "Brown" in kwargs["context"]["university"]:
                raise TimeoutError("offline fault injection")
            from research_response_fixtures import structured
            return structured(kwargs['context'], [
                WebFact(field="gre_policy", value="not_required", quote="GRE is not required.")])
    async def run():
        service = ResearchService(None, llm=Model(), web_factory=Web)
        result = ResearchResult()
        task = parse_task(request("Brown、CMU MSCS 2027 Fall GRE"))
        await service._web(task, result, targets())
        assert result.diagnostics["search_calls"] == 2
        assert result.diagnostics["page_calls"] == 2
        assert result.errors[0]["stage"] == "extract"
        assert result.errors[0]["code"] == "TimeoutError"
        assert any("Carnegie" in p.university and p.gre_policy == "not_required" for p in result.programs)
    asyncio.run(run())


def test_one_slow_school_cannot_spend_the_next_schools_allowance(monkeypatch):
    monkeypatch.setenv("RESEARCH_PER_SCHOOL_SECONDS", ".08")
    async def run():
        service = ResearchService(None, llm=SimpleNamespace(enabled=False), web_factory=Web)
        accepted = []
        async def accept(task, result, target, page, **kwargs):
            if "Brown" in target.university:
                await asyncio.sleep(.3)
            accepted.append(target.university)
        service._accept_page = accept
        result = ResearchResult()
        await service._web(parse_task(request()), result, targets())
        assert accepted == ["Carnegie Mellon University"]
        assert result.errors[0]["stage"] == "extract"
    asyncio.run(run())


def test_discovery_resizes_budget_after_learning_school_count():
    from opportunity_agent.v2.research.service import DiscoveredTargets
    class Model:
        enabled = True
        def generate_structured(self, model, **kwargs):
            assert model is DiscoveredTargets
            return DiscoveredTargets(programs=targets())
    async def run():
        service = ResearchService(None, llm=Model(), web_factory=Web)
        class Catalog:
            async def identities(self): return []
            async def search(self, *args, **kwargs): return []
        service.catalog = Catalog()
        async def accept(*args): pass
        service._accept_page = accept
        result = await service.execute(request(remaining_budget_seconds=500))
        assert not result.errors, result.errors
        assert result.diagnostics["budget_school_count"] == 2
        assert result.diagnostics["budget_seconds"] == 135
    asyncio.run(run())


@pytest.mark.parametrize("failed_stage", ["search", "read_page"])
def test_search_or_page_failure_does_not_abort_other_schools(failed_stage):
    class FaultWeb(Web):
        async def search(self, query, domains):
            if failed_stage == "search" and "Brown" in query:
                self.search_calls += 1
                raise TimeoutError("offline search fault")
            return await super().search(query, domains)
        async def read(self, url, domains):
            if failed_stage == "read_page" and "brown.edu" in url:
                self.page_calls += 1
                raise TimeoutError("offline read fault")
            return await super().read(url, domains)
    async def run():
        service = ResearchService(None, llm=SimpleNamespace(enabled=False), web_factory=FaultWeb)
        accepted = []
        async def accept(task, result, target, page, **kwargs): accepted.append(target.university)
        service._accept_page = accept
        result = ResearchResult()
        await service._web(parse_task(request()), result, targets())
        assert accepted == ["Carnegie Mellon University"]
        assert result.errors[0]["stage"] == failed_stage
    asyncio.run(run())


def test_repairs_prioritize_unattempted_targets_and_skip_seen_pages():
    async def run():
        service = ResearchService(None, llm=SimpleNamespace(enabled=False), web_factory=Web)
        accepted = []
        async def accept(task, result, target, page, **kwargs):
            accepted.append(target.university)
            if "Brown" in target.university:
                raise TimeoutError("offline fault")
        service._accept_page = accept
        task = parse_task(request())
        first = ResearchResult()
        await service._web(task, first, targets()[:1])
        saved = copy.deepcopy(first.diagnostics["web_progress"])
        second = ResearchResult()
        task.research_progress = saved
        await service._web(task, second, targets())
        assert second.diagnostics["web_targets_attempted"][0]["university"] == "Carnegie Mellon University"
        assert second.diagnostics["page_calls"] == 1
        assert accepted.count("Brown University") == 1
        assert task.research_progress == saved  # incoming metadata remains immutable
    asyncio.run(run())


def test_a2a_research_allowance_is_dynamic_and_carries_only_progress():
    execution = state()
    execution.profile_payload = {"gpa": 4}
    execution.research_result = ResearchResult(diagnostics={"web_progress": {"k": {"attempts": 1}}})
    execution._execution_deadline = time.monotonic() + 900
    req = request_from_state("research", execution)
    assert req.remaining_budget_seconds == 600
    assert req.research_progress == {"k": {"attempts": 1}}
    assert not req.profile_payload and not req.research_result
    assert "research_progress" not in request_from_state("profile", execution).wire_json()
    assert "research_progress" not in request_from_state("planning", execution).wire_json()
    execution._execution_deadline = time.monotonic() + 110
    assert 14 < request_from_state("research", execution).remaining_budget_seconds <= 15


def test_mcp_call_uses_remaining_school_allowance():
    from opportunity_agent.v2.research.web import TavilyMCP
    async def run():
        web = TavilyMCP()
        web.tools = {"tavily_search": ("tavily_search", {"properties": {"query": {}}})}
        web.request_timeout_seconds = 2.5
        class Session:
            async def call_tool(self, name, arguments, **kwargs):
                assert kwargs["read_timeout_seconds"].total_seconds() == 2.5
                return SimpleNamespace(isError=False, structuredContent={"results": []})
        web.session = Session()
        assert await web._call("tavily_search", {"query": "offline"}) == {"results": []}
    asyncio.run(run())


def test_python_a2a_wait_budget_extends_beyond_old_90_seconds(monkeypatch):
    import opportunity_agent.v2.agents.a2a as a2a
    observed = []
    original_wait = asyncio.wait_for
    async def wait(awaitable, *, timeout):
        observed.append(timeout)
        return await original_wait(awaitable, timeout=timeout)
    monkeypatch.setattr(a2a.asyncio, "wait_for", wait)
    class Client:
        async def invoke(self, payload):
            import json
            req = a2a.DomainA2ARequest.model_validate_json(payload["query"])
            body = a2a.DomainA2AResponse(agent="research", run_id=req.run_id, request_id=req.request_id,
                result=ResearchResult(status="failed").model_dump()).model_dump_json()
            return SimpleNamespace(artifacts=[SimpleNamespace(parts=[SimpleNamespace(text=body)])])
    async def run():
        agents = a2a.PythonOpenJiuwenDomainAgents(endpoints={"research": "http://offline"}, timeouts={"research": 90})
        async def client_for(*args): return Client()
        agents._client_for = client_for
        await agents.execute("research", state())
        assert observed == [610]
    asyncio.run(run())


def test_orchestrator_extends_outer_budget_and_keeps_explicit_override():
    class Agents:
        async def execute(self, agent, execution, missing_task=None):
            assert execution._execution_deadline - time.monotonic() > 1700
            return ResearchResult(status="failed")
    async def run():
        orchestrator = CustomOrchestrator(goal_parser=HeuristicGoalParser(), router=HeuristicRouter(),
            synthesizer=DeterministicSynthesizer(), agent_client=Agents(), max_rounds=3)
        result = await orchestrator.run(state())
        assert result.completion.status == "PARTIAL"
        budget = next(event.payload for event in result.events if event.type == "execution_budget_selected")
        assert budget["seconds"] == 1800
        assert not CustomOrchestrator(execution_budget_seconds=.01).dynamic_budget
    asyncio.run(run())


def test_failure_summary_distinguishes_model_and_web_errors():
    result = ResearchResult(errors=[{"stage": "extract", "code": "TimeoutError"}])
    assert "LLM" in failure_summary(result)[0]
    assert "不能视为官网不可访问" in failure_summary(result)[0]


def test_merge_keeps_old_results_and_progress_when_repair_fails():
    execution = state()
    execution.research_result = ResearchResult(programs=targets()[:1], status="partial",
        diagnostics={"web_progress": {"first": {"attempts": 1}}})
    ResultAggregator().merge_research(execution, ResearchResult(status="failed",
        diagnostics={"web_progress": {"second": {"attempts": 1}}}))
    assert len(execution.research_result.programs) == 1
    assert set(execution.research_result.diagnostics["web_progress"]) == {"first", "second"}


def test_page_extract_span_reports_sanitized_timeout(monkeypatch):
    from contextlib import contextmanager
    import opportunity_agent.v2.core.telemetry as telemetry
    class Span:
        def __init__(self): self.attributes = {}; self.events = []; self.status = None
        def set_attribute(self, key, value): self.attributes[key] = value
        def set_status(self, status): self.status = status
        def add_event(self, name, values): self.events.append((name, values))
    captured = Span()
    class Tracer:
        @contextmanager
        def start_as_current_span(self, *args, **kwargs):
            assert kwargs["record_exception"] is False
            yield captured
    monkeypatch.setattr(telemetry, "configure_telemetry", lambda: None)
    monkeypatch.setattr(telemetry.trace, "get_tracer", lambda *args: Tracer())
    with pytest.raises(TimeoutError):
        with telemetry.span("research.page.extract"):
            raise TimeoutError("secret-provider-body")
    assert captured.attributes["error.code"] == "TimeoutError"
    assert captured.status.status_code.name == "ERROR"
    assert "secret-provider-body" not in repr(captured.events)
