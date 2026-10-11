import asyncio
import copy
import os
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest

from opportunity_agent.v2.research.repair import RepairDecision, ToolArguments, ToolFailure
from opportunity_agent.v2.research.tool_ledger import RedisToolLedger
from opportunity_agent.v2.research.service import ResearchService, WebExtraction, WebFact
from opportunity_agent.v2.research.task import parse_task
from opportunity_agent.v2.agents.contracts import ProgramResult, ResearchResult, ExecutionState
from opportunity_agent.v2.agents.a2a import request_from_state
from opportunity_agent.v2.agents.result_aggregation import ResultAggregator
from opportunity_agent.v2.research.web import TavilyMCP


class MemoryLedger(RedisToolLedger):
    records = {}
    def __init__(self, *args):
        super().__init__(*args)
    async def _change(self, mutate):
        state = copy.deepcopy(self.records.get(self.key, self.initial))
        value = mutate(state)
        self.records[self.key] = copy.deepcopy(state)
        self.state = state
        return value


@pytest.fixture(autouse=True)
def repair_config(monkeypatch):
    MemoryLedger.records.clear()
    monkeypatch.setenv("RESEARCH_TOOL_REPAIR_ENABLED", "1")
    monkeypatch.setenv("RESEARCH_PERSIST_FACTS", "0")
    monkeypatch.setenv("RESEARCH_PROGRESS_ENABLED", "0")
    # These tests exercise the model-extraction repair path; the deterministic
    # keyword pass would otherwise resolve their fixture pages without the model.
    monkeypatch.setenv("RESEARCH_KEYWORD_EXTRACTION", "0")
    monkeypatch.delenv("RESEARCH_PER_SCHOOL_SECONDS", raising=False)


class Model:
    enabled = True
    def __init__(self, errors=()): self.errors = list(errors); self.requests = []
    def generate_structured(self, *args, **kwargs):
        self.requests.append(kwargs)
        if self.errors: raise self.errors.pop(0)
        return WebExtraction(intake="2027 Fall", facts=[WebFact(
            field="gre_policy", value="not_required", quote="GRE is not required.")])


class Planner:
    def __init__(self, actions): self.actions = iter(actions); self.contexts = []
    async def decide(self, context, remaining):
        self.contexts.append(context)
        action = next(self.actions)
        if isinstance(action, Exception): raise action
        return action(context) if callable(action) else action


class Web:
    search_limit = page_limit = 60
    tools = {"tavily_extract": {}}
    def __init__(self, *, search_empty=False, read_error=False, text=None, url=None):
        self.search_calls = self.page_calls = self.extract_calls = 0
        self.search_empty, self.read_error = search_empty, read_error
        self.text = text or "MSCS 2027 Fall. GRE is not required."
        self.url = url or "https://brown.edu/mscs"
        self.queries = []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def search(self, query, domains):
        self.search_calls += 1; self.queries.append(query)
        return {"results": [] if self.search_empty and self.search_calls == 1 else [{"url": self.url}]}
    async def validate_url(self, url, domains):
        return await TavilyMCP.validate_url(self, url, domains)
    async def _read_direct(self, url, domains, **kwargs):
        self.page_calls += 1
        if self.read_error:
            request = httpx.Request("GET", url)
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError("blocked", request=request, response=response)
        return {"url": url, "title": "MSCS", "text": self.text}
    async def extract(self, url, domains, **kwargs):
        self.page_calls += 1; self.extract_calls += 1
        return {"url": url, "title": "MSCS", "text": self.text}
    async def read(self, url, domains):
        return await self._read_direct(url, domains)


def setup(web=None, model=None, actions=()):
    web = web or Web()
    service = ResearchService(None, llm=model or Model(), web_factory=lambda: web)
    service.tool_ledger_factory = MemoryLedger
    service.repair_planner = Planner(actions)
    service._request = SimpleNamespace(user_id="u", run_id="r", request_id="q", message="Brown MSCS 2027 Fall GRE", research_tool_state={})
    service._deadline = asyncio.get_running_loop().time() + 90
    task = parse_task(SimpleNamespace(message=service._request.message, success_criteria=None, missing_task=None))
    target = ProgramResult(university="Brown University", program="MSCS", intake="2027 Fall")
    return service, task, target


def compact(context):
    page = next(o["arguments"]["page_ref"] for o in context["observations"] if o["tool"] == "extract_program_facts")
    return RepairDecision(action="call_tool", tool="extract_program_facts",
        arguments=ToolArguments(page_ref=page, fields=["gre_policy"], profile="compact"))


def test_search_failure_changes_query_and_preserves_constraints():
    async def run():
        web = Web(search_empty=True)
        service, task, target = setup(web, actions=[RepairDecision(action="call_tool", tool="search_official_pages",
            arguments=ToolArguments(query="Brown MSCS 2027 Fall GRE admissions"))])
        result = ResearchResult()
        await service._web(task, result, [target])
        assert web.search_calls == 2 and web.queries[0] != web.queries[1]
        assert result.programs[0].gre_policy == "not_required"
        assert result.diagnostics["tool_execution"]["tools_used"] == 4
        assert result.diagnostics["tool_execution"]["decisions_used"] == 1
        context = service.repair_planner.contexts[0]
        assert context["observations"][-1]["error_code"] == "NO_RELEVANT_RESULTS"
        assert "profile_payload" not in context
    asyncio.run(run())


@pytest.mark.parametrize("planner_failure", [False, True])
def test_403_fallback_is_a_separate_counted_call(planner_failure):
    async def run():
        web = Web(read_error=True)
        action = TimeoutError() if planner_failure else RepairDecision(action="call_tool", tool="extract_official_page", arguments=ToolArguments(url=web.url))
        service, task, target = setup(web, actions=[action])
        result = ResearchResult()
        await service._web(task, result, [target])
        assert web.extract_calls == 1
        assert result.programs[0].gre_policy == "not_required"
        assert result.diagnostics["tool_execution"]["tools_used"] == 4
    asyncio.run(run())


@pytest.mark.parametrize("error", [TimeoutError(), ValueError("structured output is empty")])
def test_extraction_repair_is_compact_and_bounded(error):
    async def run():
        model = Model([error])
        service, task, target = setup(model=model, actions=[compact])
        result = ResearchResult()
        await service._web(task, result, [target])
        assert result.programs[0].gre_policy == "not_required"
        assert len(model.requests) == 2
        assert all(r["allow_format_fallback"] is False for r in model.requests)
        assert result.diagnostics["tool_execution"]["tools_used"] == 4
    asyncio.run(run())


def test_bad_decisions_are_feedback_and_do_not_invoke_tools():
    async def run():
        service, task, target = setup(Web(search_empty=True), actions=[
            {"action": "call_tool", "tool": "run_shell", "arguments": {}},
            RepairDecision(action="call_tool", tool="search_official_pages", arguments=ToolArguments(query="Brown GRE 2027 Fall"))])
        result = ResearchResult()
        await service._web(task, result, [target])
        assert result.diagnostics["tool_execution"]["tools_used"] == 4
        assert result.diagnostics["tool_execution"]["decisions_used"] == 2
        assert service.repair_planner.contexts[-1]["observations"][-1]["error_code"] == "INVALID_REPAIR_DECISION"
    asyncio.run(run())


def test_year_mismatch_does_not_call_extraction_model():
    async def run():
        model = Model()
        service, task, target = setup(Web(text="MSCS Fall 2026 GRE not required."), model,
            [RepairDecision(action="skip_target")])
        result = ResearchResult()
        await service._web(task, result, [target])
        assert not model.requests and not result.programs
        assert any(e["code"] == "INTAKE_UNSUPPORTED" for e in result.errors)
    asyncio.run(run())


def test_failed_signature_and_budget_survive_rounds():
    async def run():
        ledger = MemoryLedger("u", "r", 1)
        await ledger.initialize()
        call = await ledger.reserve("Brown", tool="read", signature="s")
        await ledger.finish("Brown", call, 10, {"status": "failed", "retryable": False}, "s")
        incoming = ResearchResult(diagnostics={"tool_execution": ledger.state,
            "repair_history": [{"call_id": "a"}]})
        state = ExecutionState(user_id="u", conversation_id="c", run_id="r", request_id="q", message="Brown GRE")
        aggregator = ResultAggregator()
        aggregator.merge_research(state, incoming); aggregator.merge_research(state, incoming)
        request = request_from_state("research", state)
        assert request.research_tool_state["tools_used"] == 1
        assert len(state.research_result.diagnostics["repair_history"]) == 1
        assert "research_tool_state" not in request_from_state("profile", state).wire_json()
        restored = MemoryLedger("u", "r", 10, request.research_tool_state)
        await restored.initialize()
        assert restored.state["tool_limit"] == 6
        assert restored.remaining("Brown") == 80
        with pytest.raises(ToolFailure, match="REPEATED_FAILED_CALL"):
            await restored.reserve("Brown", tool="read", signature="s", retry=True)
        for i in range(5):
            call = await restored.reserve("Brown", tool="search", signature=str(i))
            await restored.finish("Brown", call, 0)
        with pytest.raises(ToolFailure, match="TOOL_BUDGET_EXHAUSTED"):
            await restored.reserve("Brown", tool="search", signature="last")
        await restored.close(); await ledger.close()
    asyncio.run(run())


def test_transient_retry_only_once_and_page_extraction_limit():
    async def run():
        ledger = MemoryLedger("u", "r", 1)
        await ledger.initialize()
        for i in range(2):
            call = await ledger.reserve("b", tool="read", signature="s", retry=True)
            await ledger.finish("b", call, 0, {"status": "failed", "retryable": True}, "s")
        with pytest.raises(ToolFailure, match="REPEATED_FAILED_CALL"):
            await ledger.reserve("b", tool="read", signature="s", retry=True)
        for i in range(2):
            call = await ledger.reserve("b", tool="extract", signature=str(i), extraction_key="page")
            await ledger.finish("b", call, 0)
        with pytest.raises(ToolFailure, match="PAGE_EXTRACTION_LIMIT"):
            await ledger.reserve("b", tool="extract", signature="third", extraction_key="page")
        await ledger.close()
    asyncio.run(run())


def test_circuit_resets_and_half_open_once():
    async def run():
        ledger = MemoryLedger("u", "r", 1)
        await ledger.initialize()
        await ledger.circuit("m", "failure"); await ledger.circuit("m", "ok")
        for _ in range(3): await ledger.circuit("m", "failure")
        with pytest.raises(ToolFailure, match="EXTRACTION_CIRCUIT_OPEN"):
            await ledger.circuit("m", "check", minimum_remaining=50)
        await ledger._change(lambda s: s["circuit"]["m"].update(opened_at=time.time()-31))
        await ledger.circuit("m", "check", minimum_remaining=50)
        with pytest.raises(ToolFailure, match="EXTRACTION_CIRCUIT_OPEN"):
            await ledger.circuit("m", "check", minimum_remaining=50)
        await ledger.close()
    asyncio.run(run())


def test_https_domain_and_private_ip_guards(monkeypatch):
    def public(host):
        if host == "private.brown.edu": raise ValueError("private")
    monkeypatch.setattr("opportunity_agent.v2.research.web._assert_public_host", public)
    async def run():
        web = TavilyMCP()
        for url, code in [("http://brown.edu/a", "HTTPS_REQUIRED"), ("https://evil.test/a", "OFFICIAL_DOMAIN_REJECTED"),
                          ("https://private.brown.edu/a", "PRIVATE_ADDRESS_REJECTED"),
                          ("https://brown.edu:80/a", "PORT_REJECTED")]:
            with pytest.raises(ToolFailure, match=code): await web.validate_url(url, ["brown.edu"])
        assert await web.validate_url("https://cs.brown.edu/a", ["brown.edu"]) == {"valid": True}
    asyncio.run(run())


def test_redis_unavailable_stops_before_web_connection():
    class BrokenLedger(MemoryLedger):
        async def initialize(self): raise ToolFailure("LEDGER_UNAVAILABLE")
    async def run():
        service, task, target = setup()
        service.tool_ledger_factory = BrokenLedger
        result = ResearchResult()
        await service._web(task, result, [target])
        assert service.web_factory().search_calls == 0
        assert result.errors[0]["code"] == "LEDGER_UNAVAILABLE"
    asyncio.run(run())


def test_step_budget_stops_without_extra_planner_calls():
    class DifferentPages(Web):
        async def search(self, query, domains):
            self.search_calls += 1
            return {"results": [{"url": f"https://brown.edu/mscs/{self.search_calls}"}]}
    async def run():
        actions = [RepairDecision(action="call_tool", tool="search_official_pages",
            arguments=ToolArguments(query="Brown 2027 MSCS GRE policy"))]
        service, task, target = setup(DifferentPages(), Model([ValueError("structured output is empty")]*3), actions)
        result = ResearchResult()
        await service._web(task, result, [target])
        assert result.diagnostics["tool_execution"]["tools_used"] == 6
        assert result.diagnostics["tool_execution"]["decisions_used"] == 1
        assert result.errors[-1]["code"] == "TOOL_BUDGET_EXHAUSTED"
    asyncio.run(run())


def test_disabled_path_never_uses_ledger(monkeypatch):
    monkeypatch.setenv("RESEARCH_TOOL_REPAIR_ENABLED", "0")
    async def run():
        service, task, target = setup()
        service.tool_ledger_factory = lambda *args: pytest.fail("disabled path touched ledger")
        result = ResearchResult()
        await service._web(task, result, [target])
        assert result.programs[0].gre_policy == "not_required"
        assert "tool_execution" not in result.diagnostics
    asyncio.run(run())


def test_redirect_rejection_cannot_fall_back_to_mcp(monkeypatch):
    monkeypatch.setattr("opportunity_agent.v2.research.web._assert_public_host", lambda host: None)
    monkeypatch.delenv("RESEARCH_WEB_PROXY", raising=False)
    requested = []
    def response(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.test/2027"})
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(response), **kwargs))
    async def run():
        web = TavilyMCP()
        web.tools = {"tavily_extract": {}}
        with pytest.raises(ToolFailure, match="OFFICIAL_DOMAIN_REJECTED"):
            await web._read_direct("https://brown.edu/a", ["brown.edu"], precise_errors=True, allow_body_fallback=False)
        assert requested == ["https://brown.edu/a"]
    asyncio.run(run())


def test_school_time_is_preserved_and_stops_next_round():
    async def run():
        ledger = MemoryLedger("u", "r", 1)
        await ledger.initialize()
        call = await ledger.reserve("b", tool="read", signature="s")
        await ledger.finish("b", call, 91)
        restored = MemoryLedger("u", "r", 10)
        await restored.initialize()
        assert restored.remaining("b") == 0
        with pytest.raises(ToolFailure, match="SCHOOL_BUDGET_EXHAUSTED"):
            await restored.reserve("b", tool="read", signature="other")
        await restored.close(); await ledger.close()
    asyncio.run(run())


def test_discovery_search_failure_is_repaired_without_promoting_snippets():
    class DiscoveryModel(Model):
        def generate_structured(self, schema, **kwargs):
            from opportunity_agent.v2.research.service import DiscoveredTargets
            if schema is DiscoveredTargets:
                return DiscoveredTargets(programs=[ProgramResult(university="Brown University", program="MSCS")])
            return super().generate_structured(schema, **kwargs)
    async def run():
        service, task, _ = setup(Web(search_empty=True), DiscoveryModel(), [RepairDecision(action="call_tool",
            tool="search_official_pages", arguments=ToolArguments(query="2027 Fall United States MSCS official admissions"))])
        task.entities.university = ""; task.entities.universities = []; task.entities.targets = []
        task.entities.program = ""; task.entities.programs = []
        result = ResearchResult()
        await service._web(task, result, [])
        assert result.programs[0].gre_policy == "not_required"
        calls = result.diagnostics["tool_execution"]["calls"]
        assert [c["tool"] for c in calls[:3]] == ["search_official_pages", "search_official_pages", "extract_program_facts"]
        assert result.diagnostics["tool_execution"]["tools_used"] == 6
    asyncio.run(run())


def test_unchanged_extraction_retry_is_rejected():
    def unchanged(context):
        previous = next(o for o in context["observations"] if o["tool"] == "extract_program_facts")
        return RepairDecision(action="call_tool", tool="extract_program_facts",
            arguments=ToolArguments.model_validate(previous["arguments"]))
    async def run():
        model = Model([TimeoutError()])
        service, task, target = setup(model=model, actions=[unchanged, compact])
        result = ResearchResult()
        await service._web(task, result, [target])
        assert len(model.requests) == 2
        assert result.diagnostics["tool_execution"]["tools_used"] == 4
        assert result.diagnostics["tool_execution"]["decisions_used"] == 2
        assert result.diagnostics["repair_history"][0]["invalid"]
    asyncio.run(run())


@pytest.mark.parametrize("year_only", [False, True])
def test_source_supported_school_or_year_only_scope(year_only):
    class NamedModel(Model):
        def generate_structured(self, *args, **kwargs):
            return super().generate_structured(*args, **kwargs).model_copy(update={"program": "MSCS"})
    async def run():
        service, task, target = setup(model=NamedModel())
        if year_only:
            target.intake = "2027"; task.entities.intake = "2027"
        else:
            target.program = ""; task.entities.program = ""; task.entities.programs = []
        result = ResearchResult()
        await service._web(task, result, [target])
        assert result.programs[0].gre_policy == "not_required"
        assert result.diagnostics["tool_execution"]["tools_used"] == 3
        assert not result.errors
    asyncio.run(run())


@pytest.mark.skipif(not os.getenv("V2_TEST_REDIS_URL"), reason="Explicit disposable Redis integration endpoint required")
def test_real_redis_atomic_global_limit():
    async def run():
        from redis.asyncio import Redis
        client = Redis.from_url(os.environ["V2_TEST_REDIS_URL"])
        ledger = RedisToolLedger("test", uuid.uuid4().hex, 1, client=client)
        try:
            await ledger.initialize()
            async def attempt(i):
                try:
                    return await ledger.reserve(str(i), tool="test", signature=str(i))
                except ToolFailure:
                    return None
            results = await asyncio.gather(*(attempt(i) for i in range(8)))
            await ledger.initialize()
            assert len([r for r in results if r]) == ledger.state["tools_used"] == 6
            assert await client.ttl(ledger.key) > 3600
        finally:
            await client.delete(ledger.key)  # Only this test's unique key.
            await ledger.close()
    asyncio.run(run())


@pytest.mark.parametrize("query", ["CMU MSCS 2027 Fall GRE", "Brown MSCS 2026 Fall GRE",
    "Brown MSCS 2027 Spring GRE", "Brown MSML 2027 Fall GRE", "Brown Canada 2027 GRE",
    "这些学校 GRE", "those universities GRE"])
def test_rewrite_scope_rejects_conflicts(query):
    from opportunity_agent.v2.research.search_scope import scoped_query
    async def run():
        _, task, target = setup()
        task.entities.country = "US"; task.entities.countries = ["US"]
        with pytest.raises(ToolFailure, match="QUERY_SCOPE_REJECTED"):
            scoped_query(task, target, query)
    asyncio.run(run())


def test_valid_rewrite_fills_scope_without_matching_it_inside_university():
    from opportunity_agent.v2.research.search_scope import scoped_query
    async def run():
        _, task, target = setup()
        query = scoped_query(task, target, "Brown University graduate GRE requirements")
        assert "2027 Fall" in query and "MSCS" in query
        assert scoped_query(task, target, query) == query
    asyncio.run(run())


def test_invalid_scope_decision_is_feedback_not_an_executed_query():
    async def run():
        service, task, target = setup(Web(search_empty=True), actions=[
            RepairDecision(action="call_tool", tool="search_official_pages", arguments=ToolArguments(query="CMU MSCS 2026 GRE")),
            RepairDecision(action="call_tool", tool="search_official_pages", arguments=ToolArguments(query="Brown graduate GRE policy"))])
        result = ResearchResult()
        await service._web(task, result, [target])
        assert result.programs[0].gre_policy == "not_required"
        assert len(service.web_factory().queries) == 2
        assert all("CMU" not in q and "2026" not in q for q in service.web_factory().queries)
        assert service.repair_planner.contexts[-1]["observations"][-1]["error_code"] == "QUERY_SCOPE_REJECTED"
    asyncio.run(run())


def test_ranked_candidates_use_https_and_correct_program_without_planner():
    class Candidates(Web):
        async def search(self, query, domains):
            self.search_calls += 1
            return {"results": [
                {"url": "https://brown.edu/medicine", "title": "Master of Science in Medicine"},
                {"url": "http://brown.edu/mscs", "title": "MSCS admissions"},
                {"url": "https://brown.edu/mscs", "title": "MSCS admissions"}]}
        async def _read_direct(self, url, domains, **kwargs):
            assert url == "https://brown.edu/mscs"
            return await super()._read_direct(url, domains, **kwargs)
    async def run():
        service, task, target = setup(Candidates())
        result = ResearchResult()
        await service._web(task, result, [target])
        assert result.programs[0].gre_policy == "not_required"
        assert result.diagnostics["tool_execution"]["tools_used"] == 3
        assert not service.repair_planner.contexts
    asyncio.run(run())


def test_mcp_fallback_does_not_wait_for_repair_model():
    async def run():
        service, task, target = setup(Web(read_error=True))
        result = ResearchResult()
        await service._web(task, result, [target])
        assert result.programs[0].gre_policy == "not_required"
        assert not service.repair_planner.contexts
        assert result.diagnostics["tool_execution"]["decisions_used"] == 0
        assert result.diagnostics["repair_history"][0]["strategy"] == "deterministic"
    asyncio.run(run())


def test_wrong_page_advances_to_next_candidate_without_repair_model():
    class Candidates(Web):
        async def search(self, query, domains):
            self.search_calls += 1
            return {"results": [{"url": "https://brown.edu/admissions/one", "title": "MSCS admissions"}, {"url": "https://brown.edu/mscs/two"}]}
        async def _read_direct(self, url, domains, **kwargs):
            page = await super()._read_direct(url, domains, **kwargs)
            if url.endswith("one"):
                page.update(title="Master of Environmental Science and Management", text="Environmental science programme 2027 Fall.")
            return page
    async def run():
        service, task, target = setup(Candidates())
        result = ResearchResult()
        await service._web(task, result, [target])
        assert result.programs[0].gre_policy == "not_required"
        assert len(service.llm.requests) == 1  # Wrong programme rejected before any model extraction.
        assert not service.repair_planner.contexts
    asyncio.run(run())


def test_planner_timeout_uses_compact_once_and_never_replays_completed_target():
    async def run():
        model = Model([TimeoutError()])
        service, task, target = setup(model=model, actions=[TimeoutError()])
        first = ResearchResult()
        await service._web(task, first, [target])
        assert first.programs[0].gre_policy == "not_required"
        assert len(model.requests) == 2
        assert len(model.requests[-1]["context"]["text"]) <= 4000
        second = ResearchResult()
        await service._web(task, second, [target])
        assert second.programs[0].gre_policy == "not_required"
        assert service.web_factory().search_calls == 1 and service.web_factory().page_calls == 1
        assert second.diagnostics["tool_execution"]["tools_used"] == 4
        assert "_progress" not in second.diagnostics["tool_execution"]
    asyncio.run(run())


def test_interrupted_round_resumes_cached_page_without_new_search_or_read():
    class InterruptLedger(MemoryLedger):
        interrupted = False
        async def save_progress(self, target_id, progress, **kwargs):
            await super().save_progress(target_id, progress, **kwargs)
            if (progress.get("next_action") or {}).get("tool") == "extract_program_facts" and not self.interrupted:
                type(self).interrupted = True
                raise asyncio.CancelledError()
    async def run():
        service, task, target = setup()
        service.tool_ledger_factory = InterruptLedger
        first = ResearchResult()
        with pytest.raises(asyncio.CancelledError):
            await service._web(task, first, [target])
        second = ResearchResult()
        await service._web(task, second, [target])
        assert service.web_factory().search_calls == service.web_factory().page_calls == 1
        assert second.programs[0].gre_policy == "not_required"
        assert second.diagnostics["tool_execution"]["tools_used"] == 3
        assert "MSCS 2027 Fall" not in str(second.diagnostics["tool_execution"])
    asyncio.run(run())


def test_extraction_payload_and_repair_payload_do_not_inject_history():
    from opportunity_agent.llm_client import LLMClient
    from opportunity_agent.llm_context import conversation_scope
    from opportunity_agent.v2.research.repair import ResearchRepairPlanner
    payloads = []
    def extract(payload):
        payloads.append(payload)
        return '{"intake":"2027 Fall","facts":[{"field":"gre_policy","value":"not_required","quote":"GRE is not required."}]}'
    def repair(payload):
        payloads.append(payload)
        return '{"action":"skip_target"}'
    async def run():
        llm = LLMClient(completion_fn=extract)
        service, task, target = setup(model=llm)
        with conversation_scope({"summary": "HISTORY-SECRET", "profile_summary": {"name": "PROFILE-SECRET"},
                                 "recent_messages": [{"role": "user", "content": "CHAT-SECRET"}]}):
            await service._web(task, ResearchResult(), [target])
            await ResearchRepairPlanner(LLMClient(completion_fn=repair)).decide({"query": "Brown GRE"}, 30)
        assert len(payloads) == 2
        assert all("SECRET" not in str(p) for p in payloads)
    asyncio.run(run())


def test_cse_full_name_is_not_mistaken_for_a_change_to_mscs():
    from opportunity_agent.v2.research.search_scope import scoped_query
    async def run():
        _, task, target = setup()
        target.program = "Master of Science in Computer Science and Engineering"
        proposed = "Brown Master of Science in Computer Science and Engineering 2027 Fall GRE"
        assert "Engineering" in scoped_query(task, target, proposed)
        with pytest.raises(ToolFailure, match="QUERY_SCOPE_REJECTED"):
            scoped_query(task, target, "Brown MSCS 2027 GRE")
    asyncio.run(run())


def test_candidate_reference_preserves_private_url_query_parameters():
    class Candidates(Web):
        async def search(self, query, domains):
            self.search_calls += 1
            return {"results": [{"url": "https://brown.edu/mscs?program=one"},
                                {"url": "https://brown.edu/mscs?program=two"}]}
        async def _read_direct(self, url, domains, **kwargs):
            if self.page_calls == 1:
                assert url.endswith("program=two")
            return await super()._read_direct(url, domains, **kwargs)
    def choose(context):
        candidates = context["observations"][0]["data"]["candidates"]
        assert all("program=" not in c["url"] for c in candidates)
        return RepairDecision(action="call_tool", tool="read_official_page",
            arguments=ToolArguments(candidate_ref=candidates[1]["candidate_ref"]))
    async def run():
        service, task, target = setup(Candidates(), Model([TimeoutError()]), [choose])
        result = ResearchResult()
        await service._web(task, result, [target])
        assert result.programs[0].gre_policy == "not_required"
        assert "program=" not in str(result.diagnostics["tool_execution"])
        assert service.web_factory().page_calls == 2
    asyncio.run(run())


def test_executor_rejects_unchanged_extraction_even_outside_planner():
    from opportunity_agent.v2.research.tool_loop import BoundedWebRunner
    async def run():
        service, task, target = setup(model=Model([TimeoutError()]), actions=[RepairDecision(action="skip_target")])
        first = ResearchResult()
        await service._web(task, first, [target])
        runner = BoundedWebRunner(service, task, ResearchResult(), [target])
        runner.target = target; runner.target_id = service._target_key(target)
        runner.school = "brown university"; runner.domains = ["brown.edu"]
        runner.ledger = MemoryLedger("u", "r", 1)
        await runner.ledger.initialize()
        runner.pages = runner.ledger.load_progress(runner.target_id)["pages"]
        failed = next(c for c in runner.ledger.state["calls"] if c["tool"] == "extract_program_facts")
        with pytest.raises(ToolFailure, match="UNCHANGED_EXTRACTION_RETRY"):
            await runner.execute(RepairDecision(action="call_tool", tool="extract_program_facts",
                arguments=ToolArguments.model_validate(failed["arguments"])))
        assert runner.ledger.state["tools_used"] == 3
        await runner.ledger.close()
    asyncio.run(run())


def test_no_actionable_targets_prevents_useless_orchestrator_rounds():
    from opportunity_agent.v2.agents.orchestrator import CustomOrchestrator, HeuristicGoalParser, HeuristicRouter, DeterministicSynthesizer
    class Agents:
        calls = 0
        async def execute(self, *args, **kwargs):
            self.calls += 1
            return ResearchResult(status="failed", diagnostics={"tool_targets_exhausted": True,
                "tool_execution": {"tools_used": 4, "tool_limit": 60, "blocked": False}})
    async def run():
        agents = Agents()
        state = ExecutionState(user_id="u", conversation_id="c", run_id="r", request_id="q",
            message="查询2027 Fall美国计算机硕士项目，筛选GRE不要求的项目，列出申请截止日期。")
        result = await CustomOrchestrator(agent_client=agents, goal_parser=HeuristicGoalParser(),
            router=HeuristicRouter(), synthesizer=DeterministicSynthesizer()).run(state)
        assert agents.calls == 1 and result.round_id == 0
        assert result.completion.status == "PARTIAL"
        assert any("无可执行" in r for r in result.completion.reasons)
    asyncio.run(run())


def test_three_repair_timeouts_stop_waiting_for_same_service():
    from opportunity_agent.v2.research.tool_loop import BoundedWebRunner
    from opportunity_agent.v2.research.repair import ToolObservation
    async def run():
        service, task, _ = setup(actions=[TimeoutError(), TimeoutError(), TimeoutError()])
        runner = BoundedWebRunner(service, task, ResearchResult(), [])
        runner.ledger = MemoryLedger("u", "r", 10)
        runner.web = Web()
        await runner.ledger.initialize()
        for school in ("Brown University", "Carnegie Mellon University", "Duke University", "Georgia Institute of Technology"):
            runner.target = ProgramResult(university=school, program="MSCS", intake="2027 Fall")
            runner.school = school.casefold(); runner.target_id = service._target_key(runner.target)
            runner.field_searches = []
            obs = ToolObservation(call_id=uuid.uuid4().hex, target_id=runner.target_id,
                tool="search_official_pages", status="failed", error_code="NO_RELEVANT_RESULTS",
                allowed_actions=["search_official_pages", "skip_target"])
            runner.observations = [obs]
            assert (await runner.repair(obs)).tool == "search_official_pages"
        assert len(service.repair_planner.contexts) == 3
        assert runner.ledger.state["decisions_used"] == 3
        assert runner.result.errors[-1]["code"] == "REPAIR_CIRCUIT_OPEN"
        await runner.ledger.close()
    asyncio.run(run())


@pytest.mark.skipif(not os.getenv("V2_TEST_REDIS_URL"), reason="Explicit disposable Redis integration endpoint required")
def test_real_redis_private_resume_state_round_trip():
    async def run():
        from redis.asyncio import Redis
        client = Redis.from_url(os.environ["V2_TEST_REDIS_URL"])
        ledger = RedisToolLedger("test-resume", uuid.uuid4().hex, 1, client=client)
        try:
            await ledger.initialize()
            progress = {"pending": ["https://brown.edu/mscs?token=PRIVATE"],
                        "pages": {"p": {"text": "exact source\n" * 2000}}, "next_action": None}
            await ledger.save_progress("brown", progress)
            restored = RedisToolLedger("test-resume", "unused", 1, client=client)
            restored.key = ledger.key
            await restored.initialize()
            assert restored.load_progress("brown") == progress
            assert "PRIVATE" not in str(restored.public_snapshot())
            assert "exact source" not in str(restored.public_snapshot())
            assert await client.ttl(ledger.key) > 3600
        finally:
            await client.delete(ledger.key)
            await ledger.close()
    asyncio.run(run())
