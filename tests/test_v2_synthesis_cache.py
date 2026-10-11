"""Regression coverage for answer preservation and durable verified web facts."""
import asyncio
import hashlib
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace
from urllib.error import HTTPError
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from opportunity_agent.llm_client import LLMClient, safe_error_details
from opportunity_agent.llm_context import conversation_scope
from opportunity_agent.v2.agents.contracts import (
    CompletionResult, Evidence, ExecutionState, ProgramResult, ResearchFact, ResearchFinding,
    ResearchResult, RouteDecision, SuccessCriteria,
)
from opportunity_agent.v2.agents.orchestrator import (
    CustomOrchestrator, DeterministicSynthesizer, HeuristicGoalParser, HeuristicRouter,
    LLMSynthesizer, SynthesizerUnavailable, _research_for_synthesis,
)
from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db.models import OfficialSource, ResearchProgram, ResearchRequirement
from opportunity_agent.v2.research.catalog import ResearchCatalog
from opportunity_agent.v2.research.fact_store import persist_verified_page
from opportunity_agent.v2.research.service import ResearchService, WebExtraction, WebFact
from opportunity_agent.v2.research.task import parse_task
from opportunity_agent.v2.research.quality import field_supported


PAGE = {"url": "https://msaii.cs.cmu.edu/apply-msaii-program", "title": "Apply to the MSAII Program",
        "text": "Master of Science in Artificial Intelligence and Innovation (MSAII). "
        "Entry in Fall 2027. The deadline to apply is December 9, 2026."}
FACT = {"field": "deadline", "value": "2026-12-09", "quote": "The deadline to apply is December 9, 2026."}


def program():
    ev = Evidence(source_id="official", url=PAGE["url"], title=PAGE["title"], excerpt=FACT["quote"],
        authority="official", program_match="exact", intake="2027 Fall", supports_fields=["deadline"],
        relevance_method="sql_exact", relevance_passed=True)
    return ProgramResult(program_id="p", university="CMU", program="MSAII", intake="2027 Fall",
        deadline=date(2026, 12, 9), evidence=[ev], facts=[ResearchFact(**{
            "field": "deadline", "value": FACT["value"], "verification_status": "verified", "evidence_ids": [ev.evidence_id]})])


def execution():
    s = ExecutionState(user_id="u", conversation_id="c", run_id="r", request_id="q", message="查询CMU MSAII截止日期")
    s.completion = CompletionResult(status="PASS")
    s.success_criteria = SuccessCriteria(evidence_required=True)
    s.research_result = ResearchResult(programs=[program()], status="complete")
    return s


@pytest.mark.parametrize("structured", [False, True])
def test_transport_retry_is_not_nested(monkeypatch, structured):
    monkeypatch.setattr("opportunity_agent.llm_client._retry_pause", lambda *a: None)
    calls = []
    class Output(BaseModel):
        value: str
    def fail(payload):
        calls.append(payload)
        raise TimeoutError("synthetic")
    client = LLMClient(completion_fn=fail, retries=1)
    with pytest.raises(TimeoutError):
        if structured:
            client.generate_structured(Output, system="test", context={})
        else:
            client.generate(system="test", user="test")
    assert len(calls) == 2


@pytest.mark.parametrize("code", [400, 401, 403])
@pytest.mark.parametrize("structured", [False, True])
def test_permanent_http_error_has_one_attempt(code, structured):
    from io import BytesIO
    calls = []
    class Output(BaseModel):
        value: str
    def fail(payload):
        calls.append(1)
        raise HTTPError("https://invalid.test", code, "bad request", {}, BytesIO(b'{}'))
    client = LLMClient(completion_fn=fail, retries=1)
    with pytest.raises(HTTPError):
        if structured:
            client.generate_structured(Output, system="test", context={})
        else:
            client.generate(system="test", user="test")
    assert len(calls) == 1


def test_retry_success_and_safe_diagnostics(monkeypatch):
    monkeypatch.setattr("opportunity_agent.llm_client._retry_pause", lambda *a: None)
    calls = []
    def complete(payload):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("secret response body")
        return {"content": "ok", "finish_reason": "stop", "usage": {"prompt_tokens": 5, "secret": "hidden"}}
    diagnostics = []
    assert LLMClient(completion_fn=complete).generate(system="test", user="test", diagnostics=diagnostics) == "ok"
    assert len(diagnostics) == 2 and diagnostics[0]["usage"] is None
    assert diagnostics[1]["usage"] == {"prompt_tokens": 5}
    assert "secret" not in json.dumps(diagnostics)


def test_exception_group_diagnostics_keep_status_without_error_body():
    exc = ExceptionGroup("secret", [HTTPError("https://secret.test", 401, "secret", {}, None)])
    assert safe_error_details(exc) == {"code": "ExceptionGroup", "causes": [{"code": "HTTPError", "http_status": 401}]}


def test_cancel_or_deadline_prevents_retry(monkeypatch):
    monkeypatch.setattr("opportunity_agent.llm_client._retry_pause", lambda *a: None)
    cancel = Event()
    calls = []
    def fail(payload):
        calls.append(1)
        cancel.set()
        raise TimeoutError("synthetic")
    client = LLMClient(completion_fn=fail)
    with pytest.raises(TimeoutError):
        client.generate(system="test", user="test", cancel_event=cancel)
    assert len(calls) == 1
    with pytest.raises(TimeoutError):
        client.generate(system="test", user="test", deadline=time.monotonic() - 1)
    assert len(calls) == 1


def test_synthesis_failure_preserves_pass_and_specific_answer(monkeypatch):
    monkeypatch.setattr("opportunity_agent.llm_client._retry_pause", lambda *a: None)
    def fail(payload):
        raise TimeoutError("synthetic")
    class Agents:
        async def execute(self, *args, **kwargs):
            return execution().research_result
    s = execution()
    out = asyncio.run(CustomOrchestrator(goal_parser=HeuristicGoalParser(), router=HeuristicRouter(),
        agent_client=Agents(), synthesizer=LLMSynthesizer(LLMClient(completion_fn=fail))).run(s))
    assert out.completion.status == "PASS"
    assert "2026-12-09" in out.answer and PAGE["url"] in out.answer
    assert any(e.type == "synthesizer_fallback" for e in out.events)
    assert any(e.type == "final_answer" for e in out.events)
    assert len(next(e.payload["attempts"] for e in out.events if e.type == "synthesizer_diagnostics")) == 2


def test_budget_fallback_does_not_downgrade_pass_and_cancel_propagates():
    class Slow:
        async def synthesize(self, state):
            await asyncio.sleep(10)
    async def run():
        s = execution()
        s._execution_deadline = time.monotonic() + .01
        control = CustomOrchestrator(synthesizer=Slow())
        answer = await control._synthesize_with_fallback(s)
        assert s.completion.status == "PASS" and FACT["value"] in answer
        s._execution_deadline = time.monotonic() + 100
        task = asyncio.create_task(control._synthesize_with_fallback(s))
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["empty", "timeout"])
def test_stage_failure_preserves_fact_answer(monkeypatch, kind):
    class Failure:
        async def synthesize(self, state):
            if kind == "timeout":
                await asyncio.sleep(10)
            return ""
    async def run():
        s = execution()
        s._execution_deadline = time.monotonic() + 5.02
        answer = await CustomOrchestrator(synthesizer=Failure())._synthesize_with_fallback(s)
        assert FACT["value"] in answer and s.completion.status == "PASS"
        assert any(e.type == "synthesizer_fallback" for e in s.events)
    asyncio.run(run())


@pytest.mark.parametrize("status", ["unknown", "stale", "conflicting"])
def test_fallback_never_promotes_unverified_facts(status):
    s = execution()
    s.completion.status = "PARTIAL"
    s.completion.reasons = ["缺少GRE政策"]
    s.research_result.programs[0].facts[0].verification_status = status
    answer = asyncio.run(DeterministicSynthesizer().synthesize(s))
    assert FACT["value"] not in answer and "缺少GRE政策" in answer


def test_synthesis_union_is_nonmutating_and_history_is_sent_once():
    s = execution()
    p = s.research_result.programs[0]
    ev = p.evidence[0]
    s.research_result.evidence = [ev]
    s.research_result.findings = [ResearchFinding(finding_id="missing", topic="test", statement="bad", evidence_ids=["absent"])]
    original = s.research_result.model_dump(mode="json")
    payload = _research_for_synthesis(s.research_result)
    assert json.dumps(payload).count(FACT["quote"]) == 1
    assert payload["programs"][0]["evidence_ids"] == [ev.evidence_id]
    assert not payload["findings"] and "diagnostics" not in payload
    assert s.research_result.model_dump(mode="json") == original
    s.research_result.evidence = []
    assert _research_for_synthesis(s.research_result)["evidence"][0]["evidence_id"] == ev.evidence_id
    s.recent_messages = [{"role": "user", "content": "unique-history-marker"}]
    calls = []
    with conversation_scope({"recent_messages": s.recent_messages}):
        asyncio.run(LLMSynthesizer(LLMClient(completion_fn=lambda p: calls.append(p) or "ok")).synthesize(s))
    assert json.dumps(calls).count("unique-history-marker") == 1


@asynccontextmanager
async def database(url):
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def request(query="查询CMU MSAII 2027截止日期"):
    return SimpleNamespace(message=query, request_id=uuid4().hex, run_id="test", success_criteria=None,
                           missing_task=None, conversation_context={}, relevant_memory={})


class Model:
    enabled = True
    def generate_structured(self, model, **kwargs):
        from research_response_fixtures import structured
        result = structured(kwargs['context'],[WebFact(**FACT)])
        for fact in result.facts:
            fact.program = fact.program_quote = 'Master of Science in Artificial Intelligence and Innovation'
            fact.degree = 'masters'
            fact.date_text = 'December 9, 2026'
        return result


class Web:
    search_limit, page_limit = 2, 5
    search_calls = page_calls = 0
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def search(self, *args):
        self.search_calls += 1
        return {"results": [{"url": PAGE["url"]}]}
    async def read(self, *args):
        self.page_calls += 1
        return PAGE


def test_first_web_lookup_commits_and_second_session_uses_sql(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_QUEUE_INGEST", "0")
    monkeypatch.setenv("RESEARCH_PERSIST_FACTS", "1")
    class ForbiddenWeb:
        def __init__(self): raise AssertionError("repeat query must not search")
    async def run():
        async with database("sqlite+aiosqlite:///" + (tmp_path / "facts.db").as_posix()) as (_, factory):
            async with factory() as session:
                first = await ResearchService(session, llm=Model(), web_factory=Web).execute(request())
                assert first.status == "complete", first.model_dump()
                assert first.diagnostics["persisted_facts"] == 1
            async with factory() as session:
                second = await ResearchService(session, llm=Model(), web_factory=ForbiddenWeb).execute(request())
                assert second.status == "complete", second.model_dump()
                assert second.diagnostics["cache_hit"] is True
                assert second.programs[0].deadline == date(2026, 12, 9)
                assert second.programs[0].program_id == first.programs[0].program_id
                assert "web_attempted" not in second.diagnostics
            async with factory.begin() as session:
                fact = await session.scalar(select(ResearchRequirement).where(ResearchRequirement.status == "verified"))
                fact.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
            async with factory() as session:
                refreshed = await ResearchService(session, llm=Model(), web_factory=Web).execute(request())
                assert refreshed.status == "complete", json.dumps(refreshed.model_dump(mode="json"), ensure_ascii=True)
                assert refreshed.diagnostics["refreshed_facts"] == 1
                assert refreshed.diagnostics["web_attempted"] is True
            async with factory() as session:
                latest = await ResearchService(session, llm=Model(), web_factory=Web).execute(request("查询CMU MSAII 2027最新官网截止日期"))
                assert latest.diagnostics["web_attempted"] is True
    asyncio.run(run())


def test_persistence_refresh_preserves_pending_and_reuses_aliases(tmp_path):
    async def run():
        async with database("sqlite+aiosqlite:///" + (tmp_path / "refresh.db").as_posix()) as (_, factory):
            async with factory.begin() as session:
                p = ResearchProgram(id="existing", university="Carnegie Mellon University",
                    program="Master of Science in Artificial Intelligence and Innovation", intake="Fall 2027")
                src = OfficialSource(id="source", source_key="source", university=p.university,
                    program=p.program, url=PAGE["url"], title=PAGE["title"])
                session.add_all([p, src])
                await session.flush()
                session.add(ResearchRequirement(id="pending", program_id=p.id, source_id=src.id,
                    field="deadline", value="2026-12-01", excerpt="unreviewed", content_hash="draft",
                    verified_at=datetime.now(timezone.utc), status="pending_review", program_match="pending_review"))
            async with factory.begin() as session:
                stored = await persist_verified_page(session, program(), PAGE, [FACT])
                assert stored["program_id"] == "existing" and stored["written"] == 1
            async with factory.begin() as session:
                fact = await session.scalar(select(ResearchRequirement).where(ResearchRequirement.status == "verified"))
                fact.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
            async with factory.begin() as session:
                stored = await persist_verified_page(session, program(), PAGE, [FACT])
                assert stored["written"] == 0 and stored["refreshed"] == 1
            async with factory() as session:
                assert (await session.get(ResearchRequirement, "pending")).status == "pending_review"
                assert await session.scalar(select(func.count()).select_from(ResearchProgram)) == 1
                rows = await ResearchCatalog(session).search(parse_task(request(), await ResearchCatalog(session).identities()))
                assert len(rows) == 1 and rows[0].deadline == date(2026, 12, 9)
    asyncio.run(run())


def test_db_failure_keeps_verified_result_and_does_not_rollback_caller(monkeypatch):
    async def fail(*args): raise RuntimeError("database unavailable")
    monkeypatch.setattr("opportunity_agent.v2.research.fact_store.persist_verified_page", fail)
    async def run():
        async with database("sqlite+aiosqlite:///:memory:") as (_, factory):
            async with factory() as session:
                result = ResearchResult()
                await ResearchService(session, llm=Model())._accept_page(parse_task(request()), result, program(), PAGE)
                assert result.programs[0].deadline == date(2026, 12, 9)
                assert result.diagnostics["persist_errors"] == [{"code": "RuntimeError"}]
                assert await session.scalar(select(func.count()).select_from(ResearchProgram)) == 0
    asyncio.run(run())


def test_redis_failure_does_not_discard_verified_facts(monkeypatch):
    monkeypatch.setenv("RESEARCH_QUEUE_INGEST", "1")
    monkeypatch.setenv("RESEARCH_PERSIST_FACTS", "0")
    class BrokenRedis:
        async def lpush(self, *args): raise OSError("redis unavailable")
        async def aclose(self): raise OSError("redis unavailable")
    monkeypatch.setattr("redis.asyncio.from_url", lambda *a, **k: BrokenRedis())
    async def run():
        result = ResearchResult()
        await ResearchService(None, llm=Model())._accept_page(parse_task(request()), result, program(), PAGE)
        assert result.programs[0].deadline == date(2026, 12, 9)
        assert len(result.diagnostics["ingest_queue_errors"]) == 2
    asyncio.run(run())


def test_invalid_fact_is_not_persisted():
    async def run():
        async with database("sqlite+aiosqlite:///:memory:") as (_, factory):
            async with factory.begin() as session:
                with pytest.raises(ValueError):
                    await persist_verified_page(session, program(), PAGE, [{**FACT, "value": "2027-12-09"}])
                assert await session.scalar(select(func.count()).select_from(ResearchRequirement)) == 0
    asyncio.run(run())


def test_shared_passage_keeps_both_field_bindings():
    async def run():
        quote = FACT["quote"] + " GRE is optional."
        page = {**PAGE, "text": PAGE["text"] + " GRE is optional."}
        facts = [{**FACT, "quote": quote}, {"field": "gre_policy", "value": "optional", "quote": quote}]
        async with database("sqlite+aiosqlite:///:memory:") as (_, factory):
            async with factory.begin() as session:
                stored = await persist_verified_page(session, program(), page, facts)
            async with factory() as session:
                row = await session.get(ResearchProgram, stored["program_id"])
                p = await ResearchCatalog(session).result(row)
                assert len(p.evidence) == 1
                assert field_supported(p, "deadline", SuccessCriteria())
                assert field_supported(p, "gre_policy", SuccessCriteria())
                prompt = _research_for_synthesis(ResearchResult(programs=[p]))
                assert json.dumps(prompt).count(quote) == 1
    asyncio.run(run())


@pytest.mark.skipif(not os.getenv("RESEARCH_TEST_POSTGRES_URL"), reason="isolated PostgreSQL test is opt-in")
def test_postgres_concurrent_writes_are_idempotent_and_conflicts_preserved():
    async def run():
        url = os.environ["RESEARCH_TEST_POSTGRES_URL"]
        admin = create_async_engine(url)
        schema = "test_synthesis_cache_" + uuid4().hex
        # The generated identifier is controlled and contains only letters/digits/_ .
        async with admin.begin() as conn:
            await conn.execute(text('CREATE SCHEMA "' + schema + '"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema + ",public"}})
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                # Explicit schema prevents checkfirst from seeing public tables.
                def create_isolated(sync_connection):
                    from sqlalchemy import MetaData
                    metadata = MetaData(schema=schema)
                    for table in Base.metadata.sorted_tables:
                        table.to_metadata(metadata, schema=schema)
                    metadata.create_all(sync_connection)
                await conn.run_sync(create_isolated)
                assert await conn.scalar(text("SELECT current_schema()")) == schema
                assert await conn.scalar(text("SELECT count(*) FROM research_programs")) == 0
            async with factory() as session:
                first = await ResearchService(session, llm=Model(), web_factory=Web).execute(request())
                assert first.status == "complete" and first.diagnostics["persisted_facts"] == 1
            class ForbiddenWeb:
                def __init__(self): raise AssertionError("PostgreSQL repeat must not search")
            async with factory() as session:
                second = await ResearchService(session, llm=Model(), web_factory=ForbiddenWeb).execute(request())
                assert second.status == "complete" and second.diagnostics["cache_hit"] is True
                assert second.programs[0].program_id == first.programs[0].program_id
                assert second.programs[0].deadline == first.programs[0].deadline
            async def write(page=PAGE, fact=FACT):
                async with factory.begin() as session:
                    return await persist_verified_page(session, program(), page, [fact])
            stored = await asyncio.gather(*(write() for _ in range(4)))
            assert len({s["program_id"] for s in stored}) == 1
            async with factory() as session:
                assert await session.scalar(select(func.count()).select_from(ResearchProgram)) == 1
                assert await session.scalar(select(func.count()).select_from(OfficialSource)) == 1
                assert await session.scalar(select(func.count()).select_from(ResearchRequirement)) == 1
            # A successful cache commit must not commit the reader's unrelated write.
            async with factory() as caller:
                caller.add(ResearchProgram(id="unrelated", university="Unrelated", program="MS", intake="2027"))
                await caller.flush()
                result = ResearchResult()
                await ResearchService(caller, llm=Model())._persist_facts(result, program(), PAGE, [FACT])
                assert result.diagnostics["refreshed_facts"] == 1
                await caller.rollback()
            async with factory() as session:
                assert await session.get(ResearchProgram, "unrelated") is None
            other_page = {**PAGE, "url": "https://msaii.cs.cmu.edu/admissions", "text": PAGE["text"].replace("December 9", "December 10")}
            other_fact = {**FACT, "value": "2026-12-10", "quote": FACT["quote"].replace("December 9", "December 10")}
            await write(other_page, other_fact)
            async with factory() as session:
                p = await session.get(ResearchProgram, stored[0]["program_id"])
                result = await ResearchCatalog(session).result(p)
                assert result.deadline is None
                assert all(f.verification_status == "conflicting" for f in result.facts)
        finally:
            await engine.dispose()
            async with admin.begin() as conn:
                await conn.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
            await admin.dispose()
    asyncio.run(run())
