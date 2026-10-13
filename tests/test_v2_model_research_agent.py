"""Recorded model decisions test the tool loop, not live model accuracy."""
import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from opportunity_agent.v2.agents.contracts import ExecutionState, RouteDecision, ResearchResult
from opportunity_agent.v2.agents.orchestrator import CustomOrchestrator, DeterministicSynthesizer, _research_for_synthesis
from opportunity_agent.v2.research.model_agent import (
    ModelResearchAgent, ResearchPlan, ResearchStep, NavigationStep, Question, Claim, Action, QuestionUpdate)
from opportunity_agent.v2.research.service import ResearchService


QUERY = "cse、uiucmcs、ucsd cse这三个项目的申请截止时间是什么？"
UCSD_URL = "https://cse.ucsd.edu/graduate/admissions"
UIUC_URL = "https://siebelschool.illinois.edu/admissions/graduate/application-deadlines"
UCSD_TEXT = ("# UCSD CSE Graduate Admissions\nThe Department of Computer Science & Engineering at UC San Diego "
             "offers MS and PhD degrees.\nThe UCSD Application for Graduate Admission opens for Fall 2027 admission "
             "from September 2, 2026 until December 16, 2026 at 11:59 p.m. PST.")
UIUC_TEXT = ("# Application Deadlines\n| Master of Computer Science in Urbana-Champaign | Fall & Spring | "
             "May 1 (Fall), October 15 (Spring) | June 30 (Fall) |")


def plan():
    return ResearchPlan(intake_source="conversation", interpretation="继承上文 2027 Fall；第一个 CSE 学校不明确。",
        questions=[Question(id="bare", original_text="cse", objective="申请截止日期", fields=["deadline"],
                            clarification="第一个 CSE 指的是哪所学校？", intake="2027 Fall"),
                   Question(id="uiuc", original_text="uiucmcs", university="UIUC", program="Master of Computer Science",
                            objective="分别查询 MCS 各项目形式的秋季申请截止日期", fields=["deadline"], intake="2027 Fall",
                            search_queries=["UIUC Master of Computer Science application deadline"], start_urls=[UIUC_URL]),
                   Question(id="ucsd", original_text="ucsd cse", university="UCSD", program="Computer Science & Engineering",
                            objective="CSE 研究生申请截止日期", fields=["deadline"], intake="2027 Fall",
                            search_queries=["UCSD CSE graduate admissions"], start_urls=[UCSD_URL])])


def request():
    return SimpleNamespace(message=QUERY, request_id="rq", run_id="run", user_id="u", success_criteria=None,
        missing_task=None, remaining_budget_seconds=300, research_progress={},
        recent_messages=[], conversation_context={"recent_messages":[
            {"role":"user", "content":"cmu MSAll 2027Fall的申请截止日期和GRE要求是什么"}]})


class RecordedWeb:
    search_calls = 0
    page_calls = 0
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def search(self, query, domains):
        self.search_calls += 1
        return {"results":[{"url":UIUC_URL if "UIUC" in query else UCSD_URL,"title":"Admissions"}],
                "diagnostic":{"search_candidates":1}}
    async def read(self, url, domains):
        self.page_calls += 1
        url=url.split('?')[0]
        if "ece.ucsd" in url:
            return {"url":url, "title":"Electrical and Computer Engineering", "text":"# ECE\nOther department.",
                    "links":[{"url":UCSD_URL,"title":"Computer Science and Engineering admissions"}]}
        return {"url":url,"title":"Application Deadlines" if url==UIUC_URL else "UCSD CSE Graduate Admissions",
                "text":UIUC_TEXT if url==UIUC_URL else UCSD_TEXT, "links":[]}


class RecordedPlanner:
    enabled = True
    def __init__(self, repair=False):
        self.calls=[]
        self.repair=repair
    def generate_structured(self, schema, **kwargs):
        self.calls.append(kwargs)
        if schema is ResearchPlan:
            p=plan()
            if self.repair:
                p.questions[-1].start_urls=["https://www.ece.ucsd.edu/graduate/admissions"]
            return p
        context=kwargs["context"]
        pages=context["pages"]
        if schema is NavigationStep:
            qid=context['question']['id']
            url=(UIUC_URL if qid=='uiuc' else UCSD_URL)+'?source=nav'
            return NavigationStep(actions=[Action(question_id=qid,tool='read',url=url)])
        if self.repair and context.get("question",{}).get("id") == "ucsd" and not any(p["url"]==UCSD_URL for p in pages):
            return ResearchStep(actions=[Action(question_id="ucsd",tool="read",url=UCSD_URL,
                                               reason="ECE 不是目标 CSE，选择正确院系官网")])
        claims=[]
        for page in pages:
            if page["url"] not in {UIUC_URL,UCSD_URL}:
                continue
            b=page["blocks"][0]
            quote = b["text"].strip()
            claims.append(Claim(question_id=page["question_id"],page_ref=page["page_ref"],block_id=b["id"],
                quote=quote,identity_quote="Master of Computer Science in Urbana-Champaign" if page["url"]==UIUC_URL else "UCSD CSE Graduate Admissions",
                answer="Urbana-Champaign 秋季申请为 May 1，页面未标年份" if page["url"]==UIUC_URL else "2027 Fall 为 2026年12月16日 23:59 PST",
                cycle_status="current_source" if page["url"]==UIUC_URL else "explicit"))
        return ResearchStep(claims=claims,updates=[QuestionUpdate(question_id=q,status="answered") for q in ["uiuc","ucsd"]])


def service(planner):
    return ResearchService(None, llm=SimpleNamespace(enabled=False), planner_llm=planner, web_factory=RecordedWeb)


@pytest.mark.parametrize("repair",[False,True])
def test_model_owns_every_question_and_tools_without_legacy_parser(monkeypatch,repair):
    monkeypatch.setenv("RESEARCH_AGENT_ENABLED","1")
    def forbidden(*args,**kwargs):
        raise AssertionError("Model path must never consult the keyword parser")
    monkeypatch.setattr("opportunity_agent.v2.research.service.parse_task",forbidden)
    planner=RecordedPlanner(repair)
    result=asyncio.run(service(planner).execute(request()))
    outcomes=result.diagnostics["question_outcomes"]
    assert len(outcomes)==3
    assert [q["status"] for q in outcomes]==["needs_user","answered","answered"]
    assert result.status=="partial"
    assert len(result.findings)==2
    assert {str(e.url) for e in result.evidence}=={UIUC_URL,UCSD_URL}
    assert result.diagnostics["task"]["routing"]["intake_source"]=="conversation"
    assert not result.diagnostics["task"]["intake_defaulted"]
    assert "recent_messages" in planner.calls[0]["context"]["conversation"]
    assert any("未明确标注" in f.statement for f in result.findings)
    state=ExecutionState(user_id="u",conversation_id="c",run_id="r",request_id="rq",message=QUERY,
        route_decision=RouteDecision(mode="delegate",agents=["research"],reason="query"),research_result=result)
    completion=CustomOrchestrator._check_completion(state)
    assert completion.status=="PARTIAL" and not completion.missing_tasks
    assert completion.reasons==["第一个 CSE 指的是哪所学校？"]
    payload=_research_for_synthesis(result)
    assert len(payload["question_outcomes"])==3
    assert payload["research_plan"]["intake_source"]=="conversation"
    fallback=asyncio.run(DeterministicSynthesizer().synthesize(state))
    assert "May 1" in fallback and "第一个 CSE" in fallback and "illinois.edu" in fallback


@pytest.mark.parametrize("invalid",["quote","page","cycle","identity"])
def test_claims_require_read_source_exact_quote_identity_and_explicit_cycle(invalid):
    svc=service(RecordedPlanner());svc._as_of=date.today()
    result=ResearchResult()
    agent=ModelResearchAgent(svc,request(),result)
    q=plan().questions[-1]
    agent.questions={q.id:q};agent.states={q.id:{"status":"researching","answers":[],"explanation":""}}
    from opportunity_agent.v2.research.extraction import blocks
    b=blocks("GRE is required.","UCSD CSE Graduate Admissions")[0]
    agent.pages={"p":{"question_id":"ucsd","url":UCSD_URL,"title":b["title"],"blocks":[b]}}
    claim=Claim(question_id="ucsd",page_ref="p",block_id=b["id"],quote="GRE is required.",
        identity_quote=b["title"],answer="必须提交GRE",cycle_status="current_source")
    if invalid=="quote":claim.quote="GRE is optional."
    if invalid=="page":claim.page_ref="not-read"
    if invalid=="cycle":claim.cycle_status="explicit"
    if invalid=="identity":claim.identity_quote="Master of ECE"
    agent.accept_claim(claim)
    assert not result.evidence and not result.findings


def test_planning_failure_does_not_silently_fall_back_or_drop_items(monkeypatch):
    class Unavailable:
        enabled=True
        def generate_structured(self,*args,**kwargs):
            raise TimeoutError("provider unavailable")
    monkeypatch.setenv("RESEARCH_AGENT_ENABLED","1")
    result=asyncio.run(service(Unavailable()).execute(request()))
    assert result.status=="failed"
    assert result.missing_items[0]["kind"]=="planning_failed"
    assert result.diagnostics["model_research"]


def test_research_transport_carries_history_but_not_profile_snapshot():
    from opportunity_agent.v2.agents.a2a import request_from_state, DomainA2ARequest
    from pydantic import ValidationError
    state=ExecutionState(user_id="u",conversation_id="c",run_id="r",request_id="rq",message=QUERY,
        conversation_context={"recent_messages":[{"role":"user","content":"2027 Fall"}],
                              "profile_summary":{"gpa":3.9},"summary":"入学季明确"})
    req=request_from_state("research",state)
    assert req.research_context["recent_messages"][0]["content"]=="2027 Fall"
    assert not req.conversation_context and not req.profile_payload and not req.recent_messages
    assert "profile_summary" not in req.research_context
    with pytest.raises(ValidationError):
        DomainA2ARequest(agent="research",user_id="u",conversation_id="c",run_id="r",request_id="rq",message=QUERY,
                         research_context={"profile_summary":{"gpa":3.9}})


def test_complete_structured_response_does_not_wait_for_gateway_done():
    import json
    from opportunity_agent.v2.research.model_agent import receive_structured_stream
    class NoEOF:
        def generate_stream(self,on_delta,**kwargs):
            payload=json.dumps(plan().model_dump())
            for char in payload:
                on_delta(char)
            raise AssertionError("Must stop after schema-validated root JSON, not wait for provider EOF")
    assert receive_structured_stream(NoEOF(),ResearchPlan).questions[1].original_text=="uiucmcs"


def test_public_search_follows_provider_redirect_but_filters_nonofficial_results():
    import httpx
    from opportunity_agent.v2.research.web import OfficialWeb
    def handler(req):
        if req.url.host=='www.bing.com':
            return httpx.Response(302,headers={'location':'https://cn.bing.com/search?format=rss'},request=req)
        xml='<rss><channel><item><title>Admissions</title><link>'+UCSD_URL+'</link><description>result</description></item>' \
            '<item><title>Other</title><link>https://example.com/claim</link></item></channel></rss>'
        return httpx.Response(200,content=xml,request=req)
    async def run():
        web=OfficialWeb()
        web.model_directed=True
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=False) as client:
            web.client=client
            result=await web.search('UCSD CSE admissions',['ucsd.edu'])
        assert [r['url'] for r in result['results']]==[UCSD_URL]
        assert result['diagnostic']['search_candidates']==1
    asyncio.run(run())


def test_navigation_context_keeps_real_links_but_bounds_body():
    svc=service(RecordedPlanner());result=ResearchResult()
    agent=ModelResearchAgent(svc,request(),result)
    q=plan().questions[-1]
    agent.questions={q.id:q};agent.states={q.id:{'status':'researching'}}
    agent.pages={'p':{'question_id':q.id,'url':'https://cse.ucsd.edu/','title':'Home','page_ref':'p',
        'read_mode':'navigation','blocks':[{'id':'b1','text':'home'},{'id':'b2','text':'more'}],
        'links':[{'title':'Admissions','url':UCSD_URL}]}}
    context=agent.page_context(q.id)
    assert len(context[0]['blocks'])==1
    assert context[0]['links']==[['Admissions','/graduate/admissions']]
    assert context[0]['link_origin']=='https://cse.ucsd.edu'


def test_deep_initial_admissions_page_keeps_answer_blocks():
    """A model-supplied deep source must not be truncated into navigation-only."""
    planner=RecordedPlanner()
    result=asyncio.run(service(planner).execute(request()))
    steps=[c['context'] for c in planner.calls if 'question' in c.get('context',{})]
    assert any(c['question']['id']=='ucsd' and c['pages'][0]['read_mode']=='evidence' for c in steps)


def test_rejected_table_claim_does_not_mark_timezone_only_answer_complete():
    class RepairCitation(RecordedPlanner):
        def __init__(self):
            super().__init__();self.tried=False
        def generate_structured(self,schema,**kwargs):
            result=super().generate_structured(schema,**kwargs)
            if schema is ResearchStep and kwargs['context'].get('question',{}).get('id')=='uiuc' and not self.tried:
                self.tried=True
                result.claims[0].quote='Master of Computer Science in Urbana-Champaign May 1 (Fall)'
            return result
    result=asyncio.run(service(RepairCitation()).execute(request()))
    assert [q['status'] for q in result.diagnostics['question_outcomes']]==['needs_user','answered','answered']
    assert any(o.get('error')=='quote_not_in_source_block' for o in result.diagnostics['agent_observations'])


def test_source_table_layout_repair_never_changes_words_or_dates():
    from opportunity_agent.v2.research.model_agent import source_substring
    raw='| Master of Computer Science | Fall & Spring |\nMay 1 (Fall)\nOctober 15 (Spring) |'
    quote='Master of Computer Science | Fall & Spring | May 1 (Fall)'
    matched=source_substring(raw,quote)
    assert matched in raw and '\nMay 1' in matched
    assert not source_substring(raw,quote.replace('May 1','May 2'))
    assert not source_substring(raw,'Master of Computer Science May 1 (Fall)')
    assert source_substring(raw,quote.replace(' | May',' |\\nMay')) in raw
