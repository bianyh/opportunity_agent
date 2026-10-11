"""Connectivity acceptance, not a production RAG quality benchmark.

Real: HTTP authentication/runs/SSE/storage, three Python A2A servers, domain
agents, SQL catalogue, retriever/filter/fusion, aggregation and LLMSynthesizer.
Controlled: model text, embedding/reranking scores and external web responses.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import socket
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from opportunity_agent.llm_client import LLMClient
from opportunity_agent.v2.agents.a2a import OpenJiuwenDomainAgents, create_domain_a2a_server
from opportunity_agent.v2.agents.orchestrator import CustomOrchestrator, HeuristicGoalParser, HeuristicRouter, LLMSynthesizer
from opportunity_agent.v2.agents.result_aggregation import ResultAggregator
from opportunity_agent.v2.core import config
from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db.models import AgentRun, KnowledgeChunk, KnowledgeDocument, Message, OfficialSource, Profile, ResearchProgram, ResearchRequirement
from opportunity_agent.v2.db.session import get_session
from opportunity_agent.v2.rag.retrieval import HybridRetriever
from opportunity_agent.v2.research import service as research_service


CASES = [
    ("profile", "我的GPA是3.7", {"profile"}, None),
    ("planning", "请为我制定申请规划", {"planning"}, None),
    ("sql", "查询 CMU MSCS 2027 Fall 的截止日期和 GRE", {"research"}, "sql"),
    ("rag", "查询 CMU MSCS 2027 Fall 的机器学习课程", {"research"}, "rag"),
    ("hybrid", "查询 CMU MSCS 2027 Fall 的 GRE 和 AI课程", {"research"}, "hybrid"),
    ("mcp_web", "查询 CMU MSCS 2027 Fall 最新官网截止日期和 GRE", {"research"}, "mcp_web"),
    ("all_agents", "我的GPA是3.7，查询 CMU MSCS 2027 Fall 的 GRE 和 AI课程，并为我制定申请规划", {"profile", "research", "planning"}, "hybrid"),
    ("smalltalk", "你好，谢谢你", set(), None),
]


class ConnectivityEmbedder:
    model_name = "connectivity-only-not-e5"
    last_error = None

    def embed(self, _text):
        return [1.0, 0.0]


class ConnectivityReranker:
    model_name = "connectivity-only-not-calibrated"

    def rerank(self, _query, hits):
        for hit in hits:
            hit.score = hit.rerank_score = .95
            hit.relevance_method = "cross_encoder"
        return hits, {"reranker": self.model_name, "reranker_revision": "fixture"}


class ControlledWeb:
    search_calls = page_calls = 0
    search_limit, page_limit = 2, 5

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def search(self, _query, domains):
        self.search_calls += 1
        assert domains == ["cmu.edu"]
        return {"results": [{"url": "https://www.cmu.edu/mscs"}]}

    async def read(self, url, _domains):
        self.page_calls += 1
        return {"url": url, "title": "CMU MSCS admissions", "text":
            "CMU MSCS 2027 Fall. GRE is optional. Application deadline: December 10, 2026."}


class ControlledExtraction:
    enabled = True

    def generate_structured(self, model, **_kwargs):
        from opportunity_agent.v2.research.extraction import AdmissionExtraction
        from research_response_fixtures import structured
        assert model is AdmissionExtraction
        return structured(_kwargs['context'], [
            research_service.WebFact(field="gre_policy", value="optional", quote="GRE is optional."),
            research_service.WebFact(field="deadline", value="2026-12-10", quote="Application deadline: December 10, 2026.")])


class RecordingTextModel:
    enabled = True

    def __init__(self):
        self.contexts = []

    def generate(self, **kwargs):
        context = json.loads(kwargs["user"])
        self.contexts.append(context)
        if context["research_result"]:
            return "已汇总查询结果：[项目来源](https://www.cmu.edu/mscs)。"
        if context["profile_result"]:
            return "已识别画像更新，确认后生效。"
        if context["plan_result"]:
            return "已整理申请规划草稿，请确认后使用。"
        return "你好，不客气。"


class RecordingAggregation(ResultAggregator):
    def __init__(self):
        self.calls = []

    def merge_profile(self, state, incoming):
        self.calls.append((state.run_id, "profile"))
        return super().merge_profile(state, incoming)

    def merge_research(self, state, incoming):
        self.calls.append((state.run_id, "research"))
        return super().merge_research(state, incoming)

    def merge_plan(self, state, incoming):
        self.calls.append((state.run_id, "planning"))
        return super().merge_plan(state, incoming)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def seed(factory):
    async with factory.begin() as session:
        session.add(ResearchProgram(id="chain-program", university="CMU", program="MSCS", intake="2027 Fall"))
        session.add(OfficialSource(id="chain-source", source_key="chain-source", university="CMU", program="MSCS",
            title="CMU MSCS", url="https://www.cmu.edu/mscs"))
        await session.flush()
        for field, value, quote in [("deadline", "2026-12-10", "Application deadline: December 10, 2026."),
                                     ("gre_policy", "optional", "GRE is optional.")]:
            session.add(ResearchRequirement(program_id="chain-program", source_id="chain-source", field=field,
                value=value, date_value=date(2026, 12, 10) if field == "deadline" else None,
                qualifier=value if field == "gre_policy" else "", excerpt=quote, content_hash="chain-source-hash",
                verified_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc)+timedelta(days=30),
                program_match="exact", status="verified"))
        for ident, authority, program in [("relevant", "official", "MSCS"),
                                          ("wrong-program", "official", "MBA"),
                                          ("unofficial", "unknown", "MSCS")]:
            meta = {"school": "CMU", "program": program, "intake": "2027 Fall", "program_match": "exact",
                    "expires_at": str(date.today()+timedelta(days=30)), "embedding_model": ConnectivityEmbedder.model_name}
            session.add(KnowledgeDocument(id=ident, source_id="chain-source", title="CMU " + program,
                authority=authority, url="https://www.cmu.edu/mscs", content_hash="hash-"+ident, metadata_json=meta))
            await session.flush()
            session.add(KnowledgeChunk(id="chunk-"+ident, document_id=ident, chunk_index=0,
                content="Machine learning AI curriculum research 人工智能课程和研究方向。", embedding=[1., 0.], metadata_json=meta))


def test_http_three_a2a_agents_four_research_paths_to_frontend(monkeypatch, tmp_path):
    pytest.importorskip("openjiuwen")
    # No environment key can accidentally turn a connectivity test into paid calls.
    monkeypatch.setattr(LLMClient, "enabled", property(lambda _self: False))
    monkeypatch.setenv("RESEARCH_ALLOW_SEED_FIXTURES", "0")
    monkeypatch.setenv("RESEARCH_WEB_ENABLED", "0")
    monkeypatch.setenv("RESEARCH_QUEUE_INGEST", "0")
    url = "sqlite+aiosqlite:///"+(tmp_path/"full-chain.db").as_posix()
    monkeypatch.setattr(config, "settings", replace(config.settings, database_url=url))
    base_service = research_service.ResearchService

    class ConnectedService(base_service):
        def __init__(self, session):
            super().__init__(session, llm=ControlledExtraction(),
                retriever=HybridRetriever(session, ConnectivityEmbedder(), ConnectivityReranker()),
                web_factory=ControlledWeb, rerank=True, threshold=.9)

    monkeypatch.setattr(research_service, "ResearchService", ConnectedService)
    api = importlib.import_module("opportunity_agent.v2.api.app")

    async def scenario():
        engine = create_async_engine(url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await seed(factory)
        ports = {name: free_port() for name in ("profile", "research", "planning")}
        servers = [create_domain_a2a_server(name, port=port, backend="python") for name, port in ports.items()]
        domain = OpenJiuwenDomainAgents(endpoints={name: f"http://127.0.0.1:{port}/a2a/jsonrpc/" for name, port in ports.items()},
            backend="python")
        text_model = RecordingTextModel()
        aggregator = RecordingAggregation()
        orch = CustomOrchestrator(goal_parser=HeuristicGoalParser(), router=HeuristicRouter(),
            agent_client=domain, synthesizer=LLMSynthesizer(text_model))
        orch.aggregator = aggregator
        monkeypatch.setattr(api, "SessionLocal", factory)
        monkeypatch.setattr(api, "orchestrator", orch)

        async def sessions():
            async with factory() as session:
                yield session

        api.app.dependency_overrides[get_session] = sessions
        records = []
        try:
            for server, port in zip(servers, ports.values(), strict=True):
                await server.start(host="127.0.0.1", port=port)
            async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://chain") as client:
                assert (await client.get("/v2")).status_code == 200
                js = (await client.get("/v2/assets/app.js")).text
                assert "EventSource" in js and "run.answer" in js
                registration = await client.post("/api/v1/auth/register", json={"email": "chain@example.test", "password": "chain-password-123"})
                assert registration.status_code == 201, registration.text
                assert (await client.get("/api/v1/profile")).status_code == 200
                async with factory.begin() as session:
                    profile = await session.scalar(select(Profile))
                    profile.payload = {"major": "Computer Science", "onboarding_completed": True,
                        "target_countries": ["US"], "target_degree": "MS", "target_fields": ["AI"],
                        "graduation_year": 2027, "planned_enrollment_year": 2027}
                for name, query, agents, route in CASES:
                    conv = (await client.post("/api/v1/conversations", json={"title": name})).json()
                    path = f"/api/v1/conversations/{conv['id']}/runs"
                    body = {"message": query, "request_id": "chain-"+name}
                    started = await client.post(path, json=body)
                    assert started.status_code == 202, started.text
                    run_id = started.json()["run_id"]
                    for _ in range(1000):
                        response = await client.get(f"/api/v1/runs/{run_id}")
                        run = response.json()
                        if run["status"] in {"completed", "failed"}:
                            break
                        await asyncio.sleep(.02)
                    assert run["status"] == "completed", (name, run)
                    events = (await client.get(f"/api/v1/runs/{run_id}/events")).text
                    assert "event: run_completed" in events and "event: final_answer" in events
                    async with factory() as session:
                        persisted = await session.get(AgentRun, run_id)
                        state = persisted.graph_state
                        messages = (await session.scalars(select(Message).where(Message.conversation_id == conv["id"], Message.role == "assistant"))).all()
                        assert len(messages) == 1 and messages[0].content == run["answer"]
                    assert set(state["route_decision"]["agents"]) == agents, (name, state["route_decision"])
                    assert {agent for rid, agent in aggregator.calls if rid == run_id} == agents
                    context = text_model.contexts[-1]
                    for agent, field in [("profile", "profile_result"), ("research", "research_result"), ("planning", "plan_result")]:
                        assert (context[field] is not None) == (agent in agents), (name, field)
                    assert run["answer"] and run["answer"] == state["answer"]
                    if agents:
                        assert run["completion"]["status"] == "PASS", (name, run["completion"], run.get("research_result"))
                    if route:
                        research = run["research_result"]
                        assert research["route"] == route and research["status"] == "complete", (name, research)
                        assert "https://www.cmu.edu/mscs" in run["answer"]
                        if route in {"rag", "hybrid"}:
                            traces = research["diagnostics"]["retrieval"]
                            assert traces and all(t["candidate_ids"] == ["chunk-relevant"] for t in traces)
                            assert all(t["reranker"] == ConnectivityReranker.model_name for t in traces)
                            assert research["findings"]
                        if route == "mcp_web":
                            assert research["diagnostics"]["fresh_pages"] == 1
                            assert research["diagnostics"]["search_calls"] == research["diagnostics"]["page_calls"] == 1
                    # Reposting completed work must not execute or save a second answer.
                    replay = await client.post(path, json=body)
                    assert replay.json()["run_id"] == run_id
                    records.append({"case": name, "input": query, "agents": sorted(agents), "research_route": route,
                        "completion": (run["completion"] or {}).get("status"), "answer": run["answer"],
                        "aggregation": [agent for rid, agent in aggregator.calls if rid == run_id],
                        "http": response.status_code, "sse_completed": True, "status": "PASS"})
                assert len(text_model.contexts) == len(CASES)
                async with factory() as session:
                    profile = await session.scalar(select(Profile))
                    assert "gpa" not in profile.payload, "Pending proposals must not auto-write the profile"
                print("FULL_CHAIN_RECORDS="+json.dumps(records, ensure_ascii=False))
        finally:
            api.app.dependency_overrides.clear()
            if api._run_tasks:
                await asyncio.gather(*list(api._run_tasks), return_exceptions=True)
            await domain.aclose()
            for server in servers:
                await server.stop()
            await engine.dispose()

    asyncio.run(scenario())
